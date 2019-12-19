import os
import numpy as np
from copy import deepcopy
from common.make_env import make_env
from common.yaml_ops import save_config, load_config
from Algorithms.register import get_model_info
from utils.np_utils import SMA, arrprint
from utils.replay_buffer import ExperienceReplay


def ShowConfig(config):
    for key in config:
        print('-' * 60)
        print('|', str(key).ljust(28), str(config[key]).rjust(28), '|')
    print('-' * 60)


def UpdateConfig(config, file_path, key_name='algo'):
    _config = load_config(file_path)
    try:
        for key in _config[key_name]:
            config[key] = _config[key]
    except Exception as e:
        print(e)
        sys.exit()
    return config


def get_buffer(buffer_args):
    if buffer_args['type'] == 'Pandas':
        return None
    elif buffer_args['type'] == 'ER':
        print('ER')
        from utils.replay_buffer import ExperienceReplay as Buffer
    elif buffer_args['type'] == 'PER':
        print('PER')
        from utils.replay_buffer import PrioritizedExperienceReplay as Buffer
    elif buffer_args['type'] == 'NSTEP-ER':
        print('NSTEP-ER')
        from utils.replay_buffer import NStepExperienceReplay as Buffer
    elif buffer_args['type'] == 'NSTEP-PER':
        print('NSTEP-PER')
        from utils.replay_buffer import NStepPrioritizedExperienceReplay as Buffer
    else:
        return None
    return Buffer(batch_size=buffer_args['batch_size'], capacity=buffer_args['buffer_size'], **buffer_args[buffer_args['type']])


class Agent:
    def __init__(self, env_args, model_args, buffer_args, train_args):
        self.env_args = env_args
        self.model_args = model_args
        self.buffer_args = buffer_args
        self.train_args = train_args

        self.model_index = str(self.train_args.get('index'))
        self.all_learner_print = bool(self.train_args.get('all_learner_print', False))
        self.train_args['name'] += f'-{self.model_index}'
        if self.model_args['load'] is None:
            self.train_args['load_model_path'] = os.path.join(self.train_args['base_dir'], self.train_args['name'])
        else:
            if '/' in self.model_args['load'] or '\\' in self.model_args['load']:   # 所有训练进程都以该模型路径初始化，绝对路径
                self.train_args['load_model_path'] = self.model_args['load']
            elif '-' in self.model_args['load']:
                self.train_args['load_model_path'] = os.path.join(self.train_args['base_dir'], self.model_args['load'])  # 指定了名称和序号，所有训练进程都以该模型路径初始化，相对路径
            else:   # 只写load的训练名称，不用带进程序号，会自动补
                self.train_args['load_model_path'] = os.path.join(self.train_args['base_dir'], self.model_args['load'] + f'-{self.model_index}')

        # ENV
        self.env = make_env(self.env_args)

        # ALGORITHM CONFIG
        Model, algorithm_config, _policy_mode = get_model_info(self.model_args['algo'])
        self.model_args['policy_mode'] = _policy_mode
        if self.model_args['algo_config'] is not None:
            algorithm_config = UpdateConfig(algorithm_config, self.model_args['algo_config'], 'algo')
        ShowConfig(algorithm_config)

        # BUFFER
        if _policy_mode == 'off-policy':
            self.buffer_args['batch_size'] = algorithm_config['batch_size']
            self.buffer_args['buffer_size'] = algorithm_config['buffer_size']
            _use_priority = algorithm_config.get('use_priority', False)
            _n_step = algorithm_config.get('n_step', False)
            if _use_priority and _n_step:
                self.buffer_args['type'] = 'NSTEP-PER'
                self.buffer_args['NSTEP-PER']['max_episode'] = self.train_args['max_episode']
                self.buffer_args['NSTEP-PER']['gamma'] = algorithm_config['gamma']
            elif _use_priority:
                self.buffer_args['type'] = 'PER'
                self.buffer_args['PER']['max_episode'] = self.train_args['max_episode']
            elif _n_step:
                self.buffer_args['type'] = 'NSTEP-ER'
                self.buffer_args['NSTEP-ER']['gamma'] = algorithm_config['gamma']
            else:
                self.buffer_args['type'] = 'ER'
        else:
            self.buffer_args['type'] = 'Pandas'

        # MODEL
        base_dir = os.path.join(self.train_args['base_dir'], self.train_args['name'])  # train_args['base_dir'] DIR/ENV_NAME/ALGORITHM_NAME
        if 'batch_size' in algorithm_config.keys() and train_args['fill_in']:
            self.train_args['no_op_steps'] = algorithm_config['batch_size']
        else:
            self.train_args['no_op_steps'] = train_args['no_op_steps']

        if self.env_args['type'] == 'gym':
            # buffer ------------------------------
            if 'NSTEP' in self.buffer_args['type']:
                self.buffer_args[self.buffer_args['type']]['agents_num'] = self.env_args['env_num']
            self.buffer = get_buffer(self.buffer_args)
            # buffer ------------------------------

            # model -------------------------------
            model_params = {
                's_dim': self.env.s_dim,
                'visual_sources': self.env.visual_sources,
                'visual_resolution': self.env.visual_resolution,
                'a_dim_or_list': self.env.a_dim_or_list,
                'is_continuous': self.env.is_continuous,
                'max_episode': self.train_args['max_episode'],
                'base_dir': base_dir,
                'logger2file': self.model_args['logger2file'],
                'seed': self.model_args['seed']
            }
            self.model = Model(**model_params, **algorithm_config)
            self.model.set_buffer(self.buffer)
            self.model.init_or_restore(
                os.path.join(self.train_args['load_model_path'])
            )
            # model -------------------------------

            self.train_args['begin_episode'] = self.model.get_init_episode()
            if not self.train_args['inference']:
                records_dict = {
                    'env': self.env_args,
                    'model': self.model_args,
                    'buffer': self.buffer_args,
                    'train': self.train_args,
                    'algo': algorithm_config
                }
                save_config(os.path.join(base_dir, 'config'), records_dict)
        else:
            # buffer -----------------------------------
            self.buffer_args_s = []
            for i in range(self.env.brain_num):
                _bargs = deepcopy(self.buffer_args)
                if 'NSTEP' in _bargs['type']:
                    _bargs[_bargs['type']]['agents_num'] = self.env.brain_agents[i]
                self.buffer_args_s.append(_bargs)
            buffers = [get_buffer(self.buffer_args_s[i]) for i in range(self.env.brain_num)]
            # buffer -----------------------------------

            # model ------------------------------------
            self.model_args_s = []
            for i in range(self.env.brain_num):
                _margs = deepcopy(self.model_args)
                _margs['seed'] = self.model_args['seed'] + i * 10
                self.model_args_s.append(_margs)
            model_params = [{
                's_dim': self.env.s_dim[i],
                'a_dim_or_list': self.env.a_dim_or_list[i],
                'visual_sources': self.env.visual_sources[i],
                'visual_resolution': self.env.visual_resolutions[i],
                'is_continuous': self.env.is_continuous[i],
                'max_episode': self.train_args['max_episode'],
                'base_dir': os.path.join(base_dir, b),
                'logger2file': self.model_args_s[i]['logger2file'],
                'seed': self.model_args_s[i]['seed'],    # 0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100
            } for i, b in enumerate(self.env.brain_names)]

            # multi agent training------------------------------------
            if self.model_args['algo'][:3] == 'ma_':
                self.ma = True
                assert self.env.brain_num > 1, 'if using ma* algorithms, number of brains must larger than 1'
                self.ma_data = ExperienceReplay(batch_size=10, capacity=1000)
                [mp.update({'n': self.env.brain_num, 'i': i}) for i, mp in enumerate(model_params)]
            else:
                self.ma = False
            # multi agent training------------------------------------

            self.models = [Model(
                **model_params[i],
                **algorithm_config
            ) for i in range(self.env.brain_num)]

            [model.set_buffer(buffer) for model, buffer in zip(self.models, buffers)]
            [self.models[i].init_or_restore(
                os.path.join(self.train_args['load_model_path'], b))
             for i, b in enumerate(self.env.brain_names)]
            # model ------------------------------------

            self.train_args['begin_episode'] = self.models[0].get_init_episode()
            if not self.train_args['inference']:
                for i, b in enumerate(self.env.brain_names):
                    records_dict = {
                        'env': self.env_args,
                        'model': self.model_args_s[i],
                        'buffer': self.buffer_args_s[i],
                        'train': self.train_args,
                        'algo': algorithm_config
                    }
                    save_config(os.path.join(base_dir, b, 'config'), records_dict)
        pass

    def pwi(self, *args):
        if self.all_learner_print:
            print(f'| Model-{self.model_index} |', *args)
        elif int(self.model_index) == 0:
            print(f'|#ONLY#Model-{self.model_index} |', *args)

    def __call__(self):
        self.train()

    def train(self):
        if self.env_args['type'] == 'gym':
            try:
                self.gym_no_op()
                self.gym_train()
            finally:
                self.model.close()
                self.env.close()
        else:
            try:
                if self.ma:
                    self.ma_unity_no_op()
                    self.ma_unity_train()
                else:
                    self.unity_no_op()
                    self.unity_train()
            finally:
                [model.close() for model in self.models]
                self.env.close()

    def evaluate(self):
        if self.env_args['type'] == 'gym':
            self.gym_inference()
        else:
            if self.ma:
                self.ma_unity_inference()
            else:
                self.unity_inference()

    def init_variables(self):
        """
        inputs:
            env: Environment
        outputs:
            i: specify which item of state should be modified
            state: [vector_obs, visual_obs]
            newstate: [vector_obs, visual_obs]
        """
        i = 1 if self.env.obs_type == 'visual' else 0
        return i, [np.array([[]] * self.env.n), np.array([[]] * self.env.n)], [np.array([[]] * self.env.n), np.array([[]] * self.env.n)]

    def get_visual_input(self, n, cameras, brain_obs):
        '''
        inputs:
            n: agents number
            cameras: camera number
            brain_obs: observations of specified brain, include visual and vector observation.
        output:
            [vector_information, [visual_info0, visual_info1, visual_info2, ...]]
        '''
        ss = []
        for j in range(n):
            s = []
            for k in range(cameras):
                s.append(brain_obs.visual_observations[k][j])
            ss.append(np.array(s))
        return np.array(ss)

    def gym_train(self):
        """
        Inputs:
            env:                gym environment
            gym_model:          algorithm model
            begin_episode:      initial episode
            save_frequency:     how often to save checkpoints
            max_step:           maximum number of steps in an episode
            max_episode:        maximum number of episodes in this training task
            render:             specify whether render the env or not
            render_episode:     if 'render' is false, specify from which episode to render the env
            policy_mode:        'on-policy' or 'off-policy'
        """
        begin_episode = int(self.train_args['begin_episode'])
        render = bool(self.train_args['render'])
        render_episode = int(self.train_args.get('render_episode', 50000))
        save_frequency = int(self.train_args['save_frequency'])
        max_step = int(self.train_args['max_step'])
        max_episode = int(self.train_args['max_episode'])
        eval_while_train = int(self.train_args['eval_while_train'])
        max_eval_episode = int(self.train_args.get('max_eval_episode'))
        policy_mode = str(self.model_args['policy_mode'])

        i, state, new_state = self.init_variables()
        sma = SMA(100)
        for episode in range(begin_episode, max_episode):
            state[i] = self.env.reset()
            dones_flag = np.full(self.env.n, False)
            step = 0
            r = np.zeros(self.env.n)
            last_done_step = -1
            while True:
                step += 1
                r_tem = np.zeros(self.env.n)
                if render or episode > render_episode:
                    self.env.render()
                action = self.model.choose_action(s=state[0], visual_s=state[1])
                new_state[i], reward, done, info = self.env.step(action)
                unfinished_index = np.where(dones_flag == False)[0]
                dones_flag += done
                r_tem[unfinished_index] = reward[unfinished_index]
                r += r_tem
                self.model.store_data(
                    s=state[0],
                    visual_s=state[1],
                    a=action,
                    r=reward,
                    s_=new_state[0],
                    visual_s_=new_state[1],
                    done=done
                )

                if policy_mode == 'off-policy':
                    self.model.learn(episode=episode, step=1)
                if all(dones_flag):
                    if last_done_step == -1:
                        last_done_step = step
                    if policy_mode == 'off-policy':
                        break

                if step >= max_step:
                    break

                if len(self.env.dones_index):    # 判断是否有线程中的环境需要局部reset
                    new_state[i][self.env.dones_index] = self.env.partial_reset()
                state[i] = new_state[i]

            sma.update(r)
            if policy_mode == 'on-policy':
                self.model.learn(episode=episode, step=step)
            self.model.writer_summary(
                episode,
                reward_mean=r.mean(),
                reward_min=r.min(),
                reward_max=r.max(),
                step=last_done_step,
                **sma.rs
            )
            self.pwi('-' * 40)
            self.pwi(f'Episode: {episode:3d} | step: {step:4d} | last_done_step {last_done_step:4d} | rewards: {arrprint(r, 3)}')
            if episode % save_frequency == 0:
                self.model.save_checkpoint(episode)

            if eval_while_train and self.env.reward_threshold is not None:
                if r.max() >= self.env.reward_threshold:
                    self.pwi(f'-------------------------------------------Evaluate episode: {episode:3d}--------------------------------------------------')
                    self.gym_evaluate()

    def gym_evaluate(self):
        max_step = int(self.train_args['max_step'])
        max_eval_episode = int(self.train_args['max_eval_eposide'])
        i, state, _ = self.init_variables()
        total_r = np.zeros(self.env.n)
        total_steps = np.zeros(self.env.n)
        episodes = max_eval_episode // self.env.n
        for _ in range(episodes):
            state[i] = self.env.reset()
            dones_flag = np.full(self.env.n, False)
            steps = np.zeros(self.env.n)
            r = np.zeros(self.env.n)
            while True:
                r_tem = np.zeros(self.env.n)
                action = self.model.choose_action(s=state[0], visual_s=state[1], evaluation=True)  # In the future, this method can be combined with choose_action
                state[i], reward, done, info = self.env.step(action)
                unfinished_index = np.where(dones_flag == False)
                dones_flag += done
                r_tem[unfinished_index] = reward[unfinished_index]
                steps[unfinished_index] += 1
                r += r_tem
                if all(dones_flag) or any(steps >= max_step):
                    break
            total_r += r
            total_steps += steps
        average_r = total_r.mean() / episodes
        average_step = int(total_steps.mean() / episodes)
        solved = True if average_r >= self.env.reward_threshold else False
        self.pwi(f'evaluate number: {max_eval_episode:3d} | average step: {average_step} | average reward: {average_r} | SOLVED: {solved}')
        self.pwi('----------------------------------------------------------------------------------------------------------------------------')

    def gym_no_op(self):
        steps = self.train_args['no_op_steps']
        choose = self.train_args['no_op_choose']
        assert isinstance(steps, int) and steps >= 0, 'no_op.steps must have type of int and larger than/equal 0'

        i, state, new_state = self.init_variables()

        state[i] = self.env.reset()

        steps = steps // self.env.n + 1

        for step in range(steps):
            self.pwi(f'no op step {step}')
            if choose:
                action = self.model.choose_action(s=state[0], visual_s=state[1])
            else:
                action = self.env.sample_actions()
            new_state[i], reward, done, info = self.env.step(action)
            self.model.no_op_store(
                s=state[0],
                visual_s=state[1],
                a=action,
                r=reward,
                s_=new_state[0],
                visual_s_=new_state[1],
                done=done
            )
            if len(self.env.dones_index):    # 判断是否有线程中的环境需要局部reset
                new_state[i][self.env.dones_index] = self.env.partial_reset()
            state[i] = new_state[i]

    def gym_inference(self):
        i, state, _ = self.init_variables()
        while True:
            state[i] = self.env.reset()
            while True:
                self.env.render()
                action = self.model.choose_action(s=state[0], visual_s=state[1], evaluation=True)
                state[i], reward, done, info = self.env.step(action)
                if len(self.env.dones_index):    # 判断是否有线程中的环境需要局部reset
                    state[i][self.env.dones_index] = self.env.partial_reset()

    def unity_train(self):
        """
        Train loop. Execute until episode reaches its maximum or press 'ctrl+c' artificially.
        Inputs:
            env:                    Environment for interaction.
            models:                 all models for this trianing task.
            save_frequency:         how often to save checkpoints.
            reset_config:           configuration to reset for Unity environment.
            max_step:               maximum number of steps for an episode.
            sampler_manager:        sampler configuration parameters for 'reset_config'.
            resampling_interval:    how often to resample parameters for env reset.
        Variables:
            brain_names:    a list of brain names set in Unity.
            state: store    a list of states for each brain. each item contain a list of states for each agents that controlled by the same brain.
            visual_state:   store a list of visual state information for each brain.
            action:         store a list of actions for each brain.
            dones_flag:     store a list of 'done' for each brain. use for judge whether an episode is finished for every agents.
            agents_num:     use to record 'number' of agents for each brain.
            rewards:        use to record rewards of agents for each brain.
        """
        begin_episode = int(self.train_args['begin_episode'])
        save_frequency = int(self.train_args['save_frequency'])
        max_step = int(self.train_args['max_step'])
        max_episode = int(self.train_args['max_episode'])
        policy_mode = str(self.model_args['policy_mode'])

        brains_num = len(self.env.brain_names)
        state = [0] * brains_num
        visual_state = [0] * brains_num
        action = [0] * brains_num
        dones_flag = [0] * brains_num
        agents_num = [0] * brains_num
        rewards = [0] * brains_num
        sma = [SMA(100) for i in range(brains_num)]

        for episode in range(begin_episode, max_episode):
            obs = self.env.reset()
            for i, brain_name in enumerate(self.env.brain_names):
                agents_num[i] = len(obs[brain_name].agents)
                dones_flag[i] = np.zeros(agents_num[i])
                rewards[i] = np.zeros(agents_num[i])
            step = 0
            last_done_step = -1
            while True:
                step += 1
                for i, brain_name in enumerate(self.env.brain_names):
                    state[i] = obs[brain_name].vector_observations
                    visual_state[i] = self.get_visual_input(agents_num[i], self.models[i].visual_sources, obs[brain_name])
                    action[i] = self.models[i].choose_action(s=state[i], visual_s=visual_state[i])
                actions = {f'{brain_name}': action[i] for i, brain_name in enumerate(self.env.brain_names)}
                obs = self.env.step(vector_action=actions)

                for i, brain_name in enumerate(self.env.brain_names):
                    unfinished_index = np.where(dones_flag[i] == False)[0]
                    dones_flag[i] += obs[brain_name].local_done
                    next_state = obs[brain_name].vector_observations
                    next_visual_state = self.get_visual_input(agents_num[i], self.models[i].visual_sources, obs[brain_name])
                    self.models[i].store_data(
                        s=state[i],
                        visual_s=visual_state[i],
                        a=action[i],
                        r=np.asarray(obs[brain_name].rewards),
                        s_=next_state,
                        visual_s_=next_visual_state,
                        done=np.asarray(obs[brain_name].local_done)
                    )
                    rewards[i][unfinished_index] += np.asarray(obs[brain_name].rewards)[unfinished_index]
                    if policy_mode == 'off-policy':
                        self.models[i].learn(episode=episode, step=1)

                if all([all(dones_flag[i]) for i in range(brains_num)]):
                    if last_done_step == -1:
                        last_done_step = step
                    if policy_mode == 'off-policy':
                        break

                if step >= max_step:
                    break

            for i in range(brains_num):
                sma[i].update(rewards[i])
                if policy_mode == 'on-policy':
                    self.models[i].learn(episode=episode, step=step)
                self.models[i].writer_summary(
                    episode,
                    reward_mean=rewards[i].mean(),
                    reward_min=rewards[i].min(),
                    reward_max=rewards[i].max(),
                    step=last_done_step,
                    **sma[i].rs
                )
            self.pwi('-' * 40)
            self.pwi(f'episode {episode:3d} | step {step:4d} | last_done_step {last_done_step:4d}')
            for i in range(brains_num):
                self.pwi(f'brain {i:2d} reward: {arrprint(rewards[i], 3)}')
            if episode % save_frequency == 0:
                for i in range(brains_num):
                    self.models[i].save_checkpoint(episode)

    def unity_no_op(self):
        '''
        Interact with the environment but do not perform actions. Prepopulate the ReplayBuffer.
        Make sure steps is greater than n-step if using any n-step ReplayBuffer.
        '''
        steps = self.train_args['no_op_steps']
        choose = self.train_args['no_op_choose']
        assert isinstance(steps, int) and steps >= 0, 'no_op.steps must have type of int and larger than/equal 0'

        brains_num = len(self.env.brain_names)
        state = [0] * brains_num
        visual_state = [0] * brains_num
        agents_num = [0] * brains_num
        action = [0] * brains_num
        obs = self.env.reset()

        for i, brain_name in enumerate(self.env.brain_names):
            # initialize actions to zeros
            agents_num[i] = len(obs[brain_name].agents)
            if self.env.brains[brain_name].vector_action_space_type == 'continuous':
                action[i] = np.zeros((agents_num[i], self.env.brains[brain_name].vector_action_space_size[0]), dtype=np.int32)
            else:
                action[i] = np.zeros((agents_num[i], len(self.env.brains[brain_name].vector_action_space_size)), dtype=np.int32)

        steps = steps // min(agents_num) + 1

        for step in range(steps):
            self.pwi(f'no op step {step}')
            for i, brain_name in enumerate(self.env.brain_names):
                state[i] = obs[brain_name].vector_observations
                visual_state[i] = self.get_visual_input(agents_num[i], self.models[i].visual_sources, obs[brain_name])
                if choose:
                    action[i] = self.models[i].choose_action(s=state[i], visual_s=visual_state[i])
            actions = {f'{brain_name}': action[i] for i, brain_name in enumerate(self.env.brain_names)}
            obs = self.env.step(vector_action=actions)
            for i, brain_name in enumerate(self.env.brain_names):
                next_state = obs[brain_name].vector_observations
                next_visual_state = self.get_visual_input(agents_num[i], self.models[i].visual_sources, obs[brain_name])
                self.models[i].no_op_store(
                    s=state[i],
                    visual_s=visual_state[i],
                    a=action[i],
                    r=np.asarray(obs[brain_name].rewards),
                    s_=next_state,
                    visual_s_=next_visual_state,
                    done=np.asarray(obs[brain_name].local_done)
                )

    def unity_inference(self):
        """
        inference mode. algorithm model will not be train, only used to show agents' behavior
        """
        brains_num = len(self.env.brain_names)
        state = [0] * brains_num
        visual_state = [0] * brains_num
        action = [0] * brains_num
        agents_num = [0] * brains_num
        while True:
            obs = self.env.reset()
            for i, brain_name in enumerate(self.env.brain_names):
                agents_num[i] = len(obs[brain_name].agents)
            while True:
                for i, brain_name in enumerate(self.env.brain_names):
                    state[i] = obs[brain_name].vector_observations
                    visual_state[i] = self.get_visual_input(agents_num[i], self.modes[i].visual_sources, obs[brain_name])
                    action[i] = self.modes[i].choose_action(s=state[i], visual_s=visual_state[i], evaluation=True)
                actions = {f'{brain_name}': action[i] for i, brain_name in enumerate(self.env.brain_names)}
                obs = self.env.step(vector_action=actions)

    def ma_unity_no_op(self):
        steps = self.train_args['no_op_steps']
        choose = self.train_args['no_op_choose']
        assert isinstance(steps, int), 'multi-agent no_op.steps must have type of int'
        if steps < self.ma_data.batch_size:
            steps = self.ma_data.batch_size
        brains_num = len(self.env.brain_names)
        agents_num = [0] * brains_num
        state = [0] * brains_num
        action = [0] * brains_num
        reward = [0] * brains_num
        next_state = [0] * brains_num
        dones = [0] * brains_num
        obs = self.env.reset(train_mode=False)

        for i, brain_name in enumerate(self.env.brain_names):
            agents_num[i] = len(obs[brain_name].agents)
            if self.env.brains[brain_name].vector_action_space_type == 'continuous':
                action[i] = np.zeros((agents_num[i], self.env.brains[brain_name].vector_action_space_size[0]), dtype=np.int32)
            else:
                action[i] = np.zeros((agents_num[i], len(self.env.brains[brain_name].vector_action_space_size)), dtype=np.int32)

        a = [np.asarray(e) for e in zip(*action)]
        for step in range(steps):
            print(f'no op step {step}')
            for i, brain_name in enumerate(self.env.brain_names):
                state[i] = obs[brain_name].vector_observations
                if choose:
                    action[i] = self.models[i].choose_action(s=state[i])
            actions = {f'{brain_name}': action[i] for i, brain_name in enumerate(self.env.brain_names)}
            obs = self.env.step(vector_action=actions)
            for i, brain_name in enumerate(self.env.brain_names):
                reward[i] = np.asarray(obs[brain_name].rewards)[:, np.newaxis]
                next_state[i] = obs[brain_name].vector_observations
                dones[i] = np.asarray(obs[brain_name].local_done)[:, np.newaxis]
            s = [np.asarray(e) for e in zip(*state)]
            a = [np.asarray(e) for e in zip(*action)]
            r = [np.asarray(e) for e in zip(*reward)]
            s_ = [np.asarray(e) for e in zip(*next_state)]
            done = [np.asarray(e) for e in zip(*dones)]
            self.ma_data.add(s, a, r, s_, done)

    def ma_unity_train(self):
        begin_episode = int(self.train_args['begin_episode'])
        save_frequency = int(self.train_args['save_frequency'])
        max_step = int(self.train_args['max_step'])
        max_episode = int(self.train_args['max_episode'])
        policy_mode = str(self.model_args['policy_mode'])
        assert policy_mode == 'off-policy', "multi-agents algorithms now support off-policy only."
        brains_num = len(self.env.brain_names)
        batch_size = self.ma_data.batch_size
        agents_num = [0] * brains_num
        state = [0] * brains_num
        action = [0] * brains_num
        new_action = [0] * brains_num
        next_action = [0] * brains_num
        reward = [0] * brains_num
        next_state = [0] * brains_num
        dones = [0] * brains_num

        dones_flag = [0] * brains_num
        rewards = [0] * brains_num

        for episode in range(begin_episode, max_episode):
            obs = self.env.reset()
            for i, brain_name in enumerate(self.env.brain_names):
                agents_num[i] = len(obs[brain_name].agents)
                dones_flag[i] = np.zeros(agents_num[i])
                rewards[i] = np.zeros(agents_num[i])
            step = 0
            last_done_step = -1
            while True:
                step += 1
                for i, brain_name in enumerate(self.env.brain_names):
                    state[i] = obs[brain_name].vector_observations
                    action[i] = self.models[i].choose_action(s=state[i])
                actions = {f'{brain_name}': action[i] for i, brain_name in enumerate(self.env.brain_names)}
                obs = self.env.step(vector_action=actions)

                for i, brain_name in enumerate(self.env.brain_names):
                    reward[i] = np.asarray(obs[brain_name].rewards)[:, np.newaxis]
                    next_state[i] = obs[brain_name].vector_observations
                    dones[i] = np.asarray(obs[brain_name].local_done)[:, np.newaxis]
                    unfinished_index = np.where(dones_flag[i] == False)[0]
                    dones_flag[i] += obs[brain_name].local_done
                    rewards[i][unfinished_index] += np.asarray(obs[brain_name].rewards)[unfinished_index]

                s = [np.asarray(e) for e in zip(*state)]
                a = [np.asarray(e) for e in zip(*action)]
                r = [np.asarray(e) for e in zip(*reward)]
                s_ = [np.asarray(e) for e in zip(*next_state)]
                done = [np.asarray(e) for e in zip(*dones)]
                self.ma_data.add(s, a, r, s_, done)
                s, a, r, s_, done = self.ma_data.sample()
                for i, brain_name in enumerate(self.env.brain_names):
                    next_action[i] = self.models[i].get_target_action(s=s_[:, i])
                    new_action[i] = self.models[i].choose_action(s=s[:, i], evaluation=True)
                a_ = np.asarray([np.asarray(e) for e in zip(*next_action)])
                if policy_mode == 'off-policy':
                    for i in range(brains_num):
                        self.models[i].learn(
                            episode=episode,
                            ap=np.asarray([np.asarray(e) for e in zip(*next_action[:i])]).reshape(batch_size, -1) if i != 0 else np.zeros((batch_size, 0)),
                            al=np.asarray([np.asarray(e) for e in zip(*next_action[-(brains_num - i - 1):])]).reshape(batch_size, -1) if brains_num - i != 1 else np.zeros((batch_size, 0)),
                            ss=s.reshape(batch_size, -1),
                            ss_=s_.reshape(batch_size, -1),
                            aa=a.reshape(batch_size, -1),
                            aa_=a_.reshape(batch_size, -1),
                            s=s[:, i],
                            r=r[:, i]
                        )

                if all([all(dones_flag[i]) for i in range(brains_num)]):
                    if last_done_step == -1:
                        last_done_step = step
                    if policy_mode == 'off-policy':
                        break

                if step >= max_step:
                    break

            # if train_mode == 'perEpisode':
            #     for i in range(brains_num):
            #         self.models[i].learn(episode)

            for i in range(brains_num):
                self.models[i].writer_summary(
                    episode,
                    total_reward=rewards[i].mean(),
                    step=last_done_step
                )
            self.pwi('-' * 40)
            self.pwi(f'episode {episode:3d} | step {step:4d} last_done_step | {last_done_step:4d}')
            if episode % save_frequency == 0:
                for i in range(brains_num):
                    self.models[i].save_checkpoint(episode)

    def ma_unity_inference(self):
        """
        inference mode. algorithm model will not be train, only used to show agents' behavior
        """
        brains_num = len(self.env.brain_names)
        state = [0] * brains_num
        action = [0] * brains_num
        while True:
            obs = self.env.reset()
            while True:
                for i, brain_name in enumerate(self.env.brain_names):
                    state[i] = obs[brain_name].vector_observations
                    action[i] = self.models[i].choose_action(s=state[i], evaluation=True)
                actions = {f'{brain_name}': action[i] for i, brain_name in enumerate(self.env.brain_names)}
                obs = self.env.step(vector_action=actions)
