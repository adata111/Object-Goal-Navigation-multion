import json
import time
import bz2
import gzip
import _pickle as cPickle
import gym
import numpy as np
import quaternion
import skimage.morphology
import habitat

from envs.utils.fmm_planner import FMMPlanner
from constants import coco_categories
import envs.utils.pose as pu


class ObjectGoal_Env(habitat.RLEnv):
    """The Object Goal Navigation environment class. The class is responsible
    for loading the dataset, generating episodes, and computing evaluation
    metrics.
    """

    def __init__(self, args, rank, config_env, dataset):
        self.args = args
        self.rank = rank
        print("NEW OBJECT CREATED")
        super().__init__(config_env, dataset)

        # Loading dataset info file
        self.split = config_env.DATASET.SPLIT
        self.episodes_dir = config_env.DATASET.EPISODES_DIR.format(
            split=self.split)

        dataset_info_file = self.episodes_dir + \
            "{split}_info.pbz2".format(split=self.split)
        with bz2.BZ2File(dataset_info_file, 'rb') as f:
            self.dataset_info = cPickle.load(f)

        # Specifying action and observation space
        self.action_space = gym.spaces.Discrete(3)

        self.observation_space = gym.spaces.Box(0, 255,
                                                (3, args.frame_height,
                                                 args.frame_width),
                                                dtype='uint8')

        # Initializations
        self.episode_no = 0

        # Scene info
        self.last_scene_path = None
        self.scene_path = None
        self.scene_name = None

        # Episode Dataset info
        self.eps_data = None
        self.eps_data_idx = None
        self.gt_planner = None
        self.object_boundary = None
        self.goal_idx = None
        self.goal_name = None
        self.map_obj_origin = None
        self.starting_loc = None
        self.starting_distance = None
        self.done_list = []

        # Episode tracking info
        self.curr_distance = None
        self.prev_distance = None
        self.timestep = None
        self.stopped = None
        self.path_length = None
        self.last_sim_location = None
        self.trajectory_states = []
        self.info = {}
        self.info['distance_to_goal'] = []
        self.info['spl'] = []
        self.info['success'] = []
        self.info['is_it_done'] = False
        self.info['episode_count'] =  0 

    def load_new_episode(self):
        """The function loads a fixed episode from the episode dataset. This
        function is used for evaluating a trained model on the val split.
        """

        args = self.args
        self.scene_path = self.habitat_env.sim.config.SCENE
        scene_name = self.scene_path.split("/")[-1].split(".")[0]

        if self.scene_path != self.last_scene_path:
            episodes_file = self.episodes_dir + \
                "content/{}_episodes.json.gz".format(scene_name)

            print("Loading episodes from: {}".format(episodes_file))
            with gzip.open(episodes_file, 'r') as f:
                self.eps_data = json.loads(
                    f.read().decode('utf-8'))["episodes"]

            self.eps_data_idx = 0
            self.last_scene_path = self.scene_path

        # Load episode info
        episode = self.eps_data[self.eps_data_idx]
        self.eps_data_idx += 1
        self.eps_data_idx = self.eps_data_idx % len(self.eps_data)
        pos = episode["start_position"]
        rot = quaternion.from_float_array(episode["start_rotation"])

        goal_name = episode["object_category"]
        goal_idx = episode["object_id"]
        floor_idx = episode["floor_id"]

        # Load scene info
        scene_info = self.dataset_info[scene_name]
        sem_map = scene_info[floor_idx]['sem_map']
        map_obj_origin = scene_info[floor_idx]['origin']

        # Setup ground truth planner
        object_boundary = args.success_dist
        map_resolution = args.map_resolution
        selem = skimage.morphology.disk(2)
        traversible = skimage.morphology.binary_dilation(
            sem_map[0], selem) != True
        traversible = 1 - traversible
        planner = FMMPlanner(traversible)
        selem = skimage.morphology.disk(
            int(object_boundary * 100. / map_resolution))
        goal_map = skimage.morphology.binary_dilation(
            sem_map[goal_idx + 1], selem) != True
        goal_map = 1 - goal_map
        planner.set_multi_goal(goal_map)

        # Get starting loc in GT map coordinates
        x = -pos[2]
        y = -pos[0]
        min_x, min_y = map_obj_origin / 100.0
        map_loc = int((-y - min_y) * 20.), int((-x - min_x) * 20.)

        self.gt_planner = planner
        self.starting_loc = map_loc
        self.object_boundary = object_boundary
        self.goal_idx = goal_idx
        self.goal_name = goal_name
        self.map_obj_origin = map_obj_origin

        self.starting_distance = self.gt_planner.fmm_dist[self.starting_loc]\
            / 20.0 + self.object_boundary
        self.prev_distance = self.starting_distance
        self._env.sim.set_agent_state(pos, rot)

        # The following two should match approximately
        # print(starting_loc)
        # print(self.sim_continuous_to_sim_map(self.get_sim_location()))

        obs = self._env.sim.get_observations_at(pos, rot)

        return obs

    def generate_new_episode(self):
        """The function generates a random valid episode. This function is used
        for training a model on the train split.
        """

        args = self.args
        goal_name = []
        self.scene_path = self.habitat_env.sim.config.SCENE
        self.scene_name = self.scene_path.split("/")[-1].split(".")[0]

        self.scene_info = self.dataset_info[self.scene_name]
        map_resolution = args.map_resolution

        floor_idx = np.random.randint(len(self.scene_info.keys()))
        floor_height = self.scene_info[floor_idx]['floor_height']
        self.sem_map = self.scene_info[floor_idx]['sem_map']
        map_obj_origin = self.scene_info[floor_idx]['origin']

        cat_counts = self.sem_map.sum(2).sum(1)
        possible_cats = list(np.arange(6))

        for i in range(6):
            if cat_counts[i + 1] == 0:
                possible_cats.remove(i)

        object_boundary = args.success_dist

        loc_found = False
        while not loc_found:
            if len(possible_cats) == 0:
                print("No valid objects for {}".format(floor_height))
                eps = eps - 1
                continue
            np.random.seed(int(time.time()))
            goal_idx = np.random.choice(possible_cats, size = args.obj_count, replace=False)
            # goal_idx = [3,5]

            # self.done_list = [False] * args.obj_count
            
            for i in range(args.obj_count):
                for key, value in coco_categories.items():
                    if value == goal_idx[i]:
                        goal_name.append(key)

            print(f"In this episode we wish to look for : {goal_name[0]} and {goal_name[1]}")
            selem = skimage.morphology.disk(2)
            traversible = skimage.morphology.binary_dilation(
                self.sem_map[0], selem) != True
            traversible = 1 - traversible

            planner = FMMPlanner(traversible)

            selem = skimage.morphology.disk(
                int(object_boundary * 100. / map_resolution))

            self.selem_goal = selem
                
            #Setting the ground truth for 0th goal object
            goal_map = skimage.morphology.binary_dilation(
                self.sem_map[goal_idx[0] + 1], selem) != True
            goal_map = 1 - goal_map

            planner.set_multi_goal(goal_map)

            m1 = self.sem_map[0] > 0
            m2 = planner.fmm_dist > (args.min_d - object_boundary) * 20.0
            m3 = planner.fmm_dist < (args.max_d - object_boundary) * 20.0

            possible_starting_locs = np.logical_and(m1, m2)
            possible_starting_locs = np.logical_and(
                possible_starting_locs, m3) * 1.
            if possible_starting_locs.sum() != 0:
                loc_found = True
            else:
                #might have to remove this altogether
                print("Invalid object: {} / {} / {}".format(
                    self.scene_name, floor_height, goal_name[0]))
                possible_cats.remove(goal_idx[0])
                self.scene_info[floor_idx]["sem_map"][goal_idx[0] + 1, :, :] = 0.
                self.dataset_info[self.scene_name][floor_idx][
                    "sem_map"][goal_idx[0] + 1, :, :] = 0.

        loc_found = False
        while not loc_found:
            pos = self._env.sim.sample_navigable_point()
            x = -pos[2]
            y = -pos[0]
            min_x, min_y = map_obj_origin / 100.0
            map_loc = int((-y - min_y) * 20.), int((-x - min_x) * 20.)
            if abs(pos[1] - floor_height) < args.floor_thr / 100.0 and \
                    possible_starting_locs[map_loc[0], map_loc[1]] == 1:
                loc_found = True

        agent_state = self._env.sim.get_agent_state(0)
        rotation = agent_state.rotation
        rvec = quaternion.as_rotation_vector(rotation)
        rvec[1] = np.random.rand() * 2 * np.pi
        rot = quaternion.from_rotation_vector(rvec)

        self.gt_planner = planner
        self.starting_loc = map_loc
        self.object_boundary = object_boundary
        self.goal_idx = goal_idx
        self.goal_name = goal_name
        self.map_obj_origin = map_obj_origin
        self.temp_goal_list = goal_idx
        self.temp_goal_names = goal_name

        self.starting_distance = self.gt_planner.fmm_dist[self.starting_loc] \
            / 20.0 + self.object_boundary
        self.prev_distance = self.starting_distance

        self._env.sim.set_agent_state(pos, rot)

        # The following two should match approximately
        # print(starting_loc)
        # print(self.sim_continuous_to_sim_map(self.get_sim_location()))

        obs = self._env.sim.get_observations_at(pos, rot)

        return obs

    def sim_map_to_sim_continuous(self, coords):
        """Converts ground-truth 2D Map coordinates to absolute Habitat
        simulator position and rotation.
        """
        agent_state = self._env.sim.get_agent_state(0)
        y, x = coords
        min_x, min_y = self.map_obj_origin / 100.0

        cont_x = x / 20. + min_x
        cont_y = y / 20. + min_y
        agent_state.position[0] = cont_y
        agent_state.position[2] = cont_x

        rotation = agent_state.rotation
        rvec = quaternion.as_rotation_vector(rotation)

        if self.args.train_single_eps:
            rvec[1] = 0.0
        else:
            rvec[1] = np.random.rand() * 2 * np.pi
        rot = quaternion.from_rotation_vector(rvec)

        return agent_state.position, rot

    def sim_continuous_to_sim_map(self, sim_loc):
        """Converts absolute Habitat simulator pose to ground-truth 2D Map
        coordinates.
        """
        x, y, o = sim_loc
        min_x, min_y = self.map_obj_origin / 100.0
        x, y = int((-x - min_x) * 20.), int((-y - min_y) * 20.)

        o = np.rad2deg(o) + 180.0
        return y, x, o

    def reset(self):
        """Resets the environment to a new episode.

        Returns:
            obs (ndarray): RGBD observations (4 x H x W)
            info (dict): contains timestep, pose, goal category and
                         evaluation metric info
        """
        args = self.args
        new_scene = self.episode_no % args.num_train_episodes == 0

        self.episode_no += 1
        self.info['episode_count'] = self.episode_no
        # Initializations
        self.timestep = 0
        self.stopped = False
        self.path_length = 1e-5
        self.trajectory_states = []

        if new_scene:
            obs = super().reset()
            self.scene_name = self.habitat_env.sim.config.SCENE
            print("Changing scene: {}/{}".format(self.rank, self.scene_name))

        self.scene_path = self.habitat_env.sim.config.SCENE

        if self.split == "val":
            obs = self.load_new_episode()
        else:
            obs = self.generate_new_episode()

        rgb = obs['rgb'].astype(np.uint8)
        depth = obs['depth']
        state = np.concatenate((rgb, depth), axis=2).transpose(2, 0, 1)
        self.last_sim_location = self.get_sim_location()

        # Set info
        self.info['time'] = self.timestep
        self.info['sensor_pose'] = [0., 0., 0.]
        self.info['goal_cat_id'] = self.goal_idx.copy()
        self.info['goal_name'] = self.goal_name.copy()
        temp = [False]*args.obj_count
        self.info['done_list'] = np.reshape(np.array(temp,dtype=bool), (1,args.obj_count))
        
        # self.info['obj_count'] = len(self.goal_idx)
        # self.info['count_']  = 0
        return state, self.info

    def step(self, action):
        """Function to take an action in the environment.

        Args:
            action (dict):
                dict with following keys:
                    'action' (int): 0: stop, 1: forward, 2: left, 3: right

        Returns:
            obs (ndarray): RGBD observations (4 x H x W)
            reward (float): amount of reward returned after previous action
            done (bool): whether the episode has ended
            info (dict): contains timestep, pose, goal category and
                         evaluation metric info
        """
        action = action["action"]
        if action == 0:
            self.stopped = True
            # Not sending stop to simulator, resetting manually
            action = 3

        obs, rew, done, _ = super().step(action) # TODO : Doubt what is it calling?

        # Get pose change
        dx, dy, do = self.get_pose_change()
        self.info['sensor_pose'] = [dx, dy, do]
        self.path_length += pu.get_l2_distance(0, dx, 0, dy)

        spl, success, dist = 0., 0., 0.
        if done:
            '''
            1. made a done_list to append all the done values; then set the done to False so that teh flow of program osn't distrubed
            2. temp_goal_list and temp_goal_name are temporary copies of goal_idx and goal_name (both lists) ; so that we can pop 
                and check if any more goals are left to explore
            '''
            # self.info['count_'] += 1
            # print(f"THIS IS THE DONE LIST BEFORE MESS UP : --- {self.info['done_list']}")
            
            # res = [i for i, val in enumerate(np.where(self.info['done_list'] == False)) if val]
            for i, obj_done in enumerate(self.info['done_list'][0]):
                if not obj_done:
                    self.info['done_list'][0][i] =True
                    break
            # self.info['done_list'][0][res] = True
            
            
            # self.info['done_list'] = np.append(self.info['done_list'],True)
            # print(f"THIS IS THE DONE LIST AFTER MESS UP : --- {self.info['done_list']}")
            if len(self.temp_goal_names) != 0:
                self.temp_goal_list.tolist().pop(0)
                self.temp_goal_names.pop(0)

            if len(self.temp_goal_names) != 0:
                
                goal_map = skimage.morphology.binary_dilation(
                    self.sem_map[self.temp_goal_list[0] + 1], self.selem_goal) != True
                goal_map = 1 - goal_map
                print(f"Should be : T  ,  F : -------------= {self.info['done_list']}")
                print(f"Yay! We found the previous goal. Setting goal map : {self.temp_goal_names[0]} ")
                self.gt_planner.set_multi_goal(goal_map)
                # self.info['done_list'].append(True)
                '''
                Should we set the temp_done to equal False : Lets try and find out
                '''
                if all(self.info['done_list'][0].tolist()):
                    done = True
                    self.stopped  = True
                    self.info['is_it_done'] = True
                    print("TYPE - 1 UPDATION")
                    # self.info['episode_count'] += 1
                else:
                    done = False
                    self.stopped = False
                    self.last_sim_location = self.get_sim_location()
                    self.info['time'] = self.timestep
                    dx, dy, do = self.get_pose_change()
                    self.info['sensor_pose'] = [dx, dy, do]

            else:
                print(f"Yay! We found all the goals!!! ")
                print(f"This is the done list when it has found everything : {self.info['done_list']}")
                done = True
                self.stopped = True
                self.info['is_it_done'] = True
                print("TYPE-2 UPDATION")
                # self.info['episode_count'] += 1
                # self.auto_reset_done = True

            spl, success, dist = self.get_metrics()
            self.info['distance_to_goal'].append(dist)
            self.info['spl'].append(spl)
            self.info['success'].append(success)

        rgb = obs['rgb'].astype(np.uint8)
        depth = obs['depth']
        state = np.concatenate((rgb, depth), axis=2).transpose(2, 0, 1)

        self.timestep += 1
        self.info['time'] = self.timestep

        return state, rew, done, self.info

    def get_reward_range(self):
        """This function is not used, Habitat-RLEnv requires this function"""
        return (0., 1.0)

    def get_reward(self, observations):
        curr_loc = self.sim_continuous_to_sim_map(self.get_sim_location())
        self.curr_distance = self.gt_planner.fmm_dist[curr_loc[0],
                                                      curr_loc[1]] / 20.0

        reward = (self.prev_distance - self.curr_distance) * \
            self.args.reward_coeff

        self.prev_distance = self.curr_distance
        return reward

    def get_metrics(self):
        """This function computes evaluation metrics for the Object Goal task

        Returns:
            spl (float): Success weighted by Path Length
                        (See https://arxiv.org/pdf/1807.06757.pdf)
            success (int): 0: Failure, 1: Successful
            dist (float): Distance to Success (DTS),  distance of the agent
                        from the success threshold boundary in meters.
                        (See https://arxiv.org/pdf/2007.00643.pdf)
        """
        curr_loc = self.sim_continuous_to_sim_map(self.get_sim_location())
        dist = self.gt_planner.fmm_dist[curr_loc[0], curr_loc[1]] / 20.0
        if dist == 0.0:
            success = 1
        else:
            success = 0
        spl = min(success * self.starting_distance / self.path_length, 1)
        return spl, success, dist

    def get_done(self, observations):
        if self.info['time'] >= self.args.max_episode_length - 1:
            done = False
            self.info['is_it_done'] = True
            self.info['time'] = self.timestep
            print("TYPE-3 UPDATION")
            return not done

        elif self.stopped:
            done = True
            # self.info['is_it_done'] = True
            print("TYPE-4 UPDATION")
        else:
            done = False
        return done

    def get_info(self, observations):
        """This function is not used, Habitat-RLEnv requires this function"""
        info = {}
        return info

    def get_spaces(self):
        """Returns observation and action spaces for the ObjectGoal task."""
        return self.observation_space, self.action_space

    def get_sim_location(self):
        """Returns x, y, o pose of the agent in the Habitat simulator."""

        agent_state = super().habitat_env.sim.get_agent_state(0)
        x = -agent_state.position[2]
        y = -agent_state.position[0]
        axis = quaternion.as_euler_angles(agent_state.rotation)[0]
        if (axis % (2 * np.pi)) < 0.1 or (axis %
                                          (2 * np.pi)) > 2 * np.pi - 0.1:
            o = quaternion.as_euler_angles(agent_state.rotation)[1]
        else:
            o = 2 * np.pi - quaternion.as_euler_angles(agent_state.rotation)[1]
        if o > np.pi:
            o -= 2 * np.pi
        return x, y, o

    def get_pose_change(self):
        """Returns dx, dy, do pose change of the agent relative to the last
        timestep."""
        curr_sim_pose = self.get_sim_location()
        dx, dy, do = pu.get_rel_pose_change(
            curr_sim_pose, self.last_sim_location)
        self.last_sim_location = curr_sim_pose
        return dx, dy, do
