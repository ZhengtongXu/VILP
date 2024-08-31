import wandb
import numpy as np
import torch
import collections
import pathlib
import tqdm
import dill
import math
import wandb.sdk.data_types.video as wv
from VILP.env.pusht.pusht_image_env import PushTImageEnv
#from diffusion_policy.gym_util.async_vector_env import AsyncVectorEnv
from diffusion_policy.gym_util.sync_vector_env import SyncVectorEnv
from diffusion_policy.gym_util.multistep_wrapper import MultiStepWrapper
from diffusion_policy.gym_util.video_recording_wrapper import VideoRecordingWrapper, VideoRecorder

from diffusion_policy.policy.base_image_policy import BaseImagePolicy
from diffusion_policy.common.pytorch_util import dict_apply
from diffusion_policy.env_runner.base_image_runner import BaseImageRunner
import cv2
import time
class PushtVilpRunner(BaseImageRunner):
    def __init__(self,
            output_dir,
            n_train=10,
            n_train_vis=3,
            train_start_seed=0,
            n_test=22,
            n_test_vis=6,
            legacy_test=False,
            test_start_seed=10000,
            max_steps=200,
            n_obs_steps=8,
            n_action_steps=8,
            fps=10,
            crf=22,
            render_size=96,
            past_action=False,
            tqdm_interval_sec=5.0,
            n_envs=None
        ):
        super().__init__(output_dir)
        if n_envs is None:
            n_envs = n_train + n_test

        steps_per_render = max(10 // fps, 1)
        def env_fn():
            return MultiStepWrapper(
                VideoRecordingWrapper(
                    PushTImageEnv(
                        legacy=legacy_test,
                        render_size=render_size
                    ),
                    video_recoder=VideoRecorder.create_h264(
                        fps=fps,
                        codec='h264',
                        input_pix_fmt='rgb24',
                        crf=crf,
                        thread_type='FRAME',
                        thread_count=1
                    ),
                    file_path=None,
                    steps_per_render=steps_per_render
                ),
                n_obs_steps=n_obs_steps,
                n_action_steps=n_action_steps,
                max_episode_steps=max_steps
            )

        env_fns = [env_fn] * n_envs
        env_seeds = list()
        env_prefixs = list()
        env_init_fn_dills = list()
        # train
        for i in range(n_train):
            seed = train_start_seed + i
            enable_render = i < n_train_vis

            def init_fn(env, seed=seed, enable_render=enable_render):
                # setup rendering
                # video_wrapper
                assert isinstance(env.env, VideoRecordingWrapper)
                env.env.video_recoder.stop()
                env.env.file_path = None
                if enable_render:
                    filename = pathlib.Path(output_dir).joinpath(
                        'media', wv.util.generate_id() + ".mp4")
                    filename.parent.mkdir(parents=False, exist_ok=True)
                    filename = str(filename)
                    env.env.file_path = filename

                # set seed
                assert isinstance(env, MultiStepWrapper)
                env.seed(seed)
            
            env_seeds.append(seed)
            env_prefixs.append('train/')
            env_init_fn_dills.append(dill.dumps(init_fn))

        # test
        for i in range(n_test):
            seed = test_start_seed + i
            enable_render = i < n_test_vis

            def init_fn(env, seed=seed, enable_render=enable_render):
                # setup rendering
                # video_wrapper
                assert isinstance(env.env, VideoRecordingWrapper)
                env.env.video_recoder.stop()
                env.env.file_path = None
                if enable_render:
                    filename = pathlib.Path(output_dir).joinpath(
                        'media', wv.util.generate_id() + ".mp4")
                    filename.parent.mkdir(parents=False, exist_ok=True)
                    filename = str(filename)
                    env.env.file_path = filename

                # set seed
                assert isinstance(env, MultiStepWrapper)
                env.seed(seed)
            
            env_seeds.append(seed)
            env_prefixs.append('test/')
            env_init_fn_dills.append(dill.dumps(init_fn))

        env = SyncVectorEnv(env_fns)

        # test env
        # env.reset(seed=env_seeds)
        # x = env.step(env.action_space.sample())
        # imgs = env.call('render')
        # import pdb; pdb.set_trace()

        self.env = env
        self.env_fns = env_fns
        self.env_seeds = env_seeds
        self.env_prefixs = env_prefixs
        self.env_init_fn_dills = env_init_fn_dills
        self.fps = fps
        self.crf = crf
        self.n_obs_steps = n_obs_steps
        self.n_action_steps = n_action_steps
        self.past_action = past_action
        self.max_steps = max_steps
        self.tqdm_interval_sec = tqdm_interval_sec
    
    def run(self, policy: BaseImagePolicy):
        device = policy.device
        dtype = policy.dtype
        env = self.env

        # plan for rollout
        n_envs = len(self.env_fns)
        n_inits = len(self.env_init_fn_dills)
        n_chunks = math.ceil(n_inits / n_envs)

        # allocate data
        all_video_paths = [None] * n_inits
        all_rewards = [None] * n_inits

        for chunk_idx in range(n_chunks):
            start = chunk_idx * n_envs
            end = min(n_inits, start + n_envs)
            this_global_slice = slice(start, end)
            this_n_active_envs = end - start
            this_local_slice = slice(0,this_n_active_envs)
            
            this_init_fns = self.env_init_fn_dills[this_global_slice]
            n_diff = n_envs - len(this_init_fns)
            if n_diff > 0:
                this_init_fns.extend([self.env_init_fn_dills[0]]*n_diff)
            assert len(this_init_fns) == n_envs

            # init envs
            env.call_each('run_dill_function', 
                args_list=[(x,) for x in this_init_fns])

            # start rollout
            obs = env.reset()

            past_action = None
            policy.reset()

            pbar = tqdm.tqdm(total=self.max_steps, desc=f"Eval PushtImageRunner {chunk_idx+1}/{n_chunks}", 
                leave=False, mininterval=self.tqdm_interval_sec)
            done = False
            pred_img_seq = None
            np_pred_image_seq = None
            image_buffer = []
            comp_time_buffer = []
            while not done:
                # create obs dict

                np_obs_dict = dict(obs)

                
                # device transfer
                obs_dict = dict_apply(np_obs_dict, 
                    lambda x: torch.from_numpy(x).to(
                        device=device))
                
                # run policy
                with torch.no_grad():

                    # compute the time for each step
                    # unit: second
                    time_stamp_before = time.time()
                    action_dict, pred_img_seq = policy.predict_action(obs_dict)
                    time_stamp_after = time.time()
                    comp_time_buffer.append(time_stamp_after - time_stamp_before)



                # device_transfer
                np_action_dict = dict_apply(action_dict,
                    lambda x: x.detach().to('cpu').numpy())

                action = np_action_dict['action']

                # step env
                obs, reward, done, info = env.step(action)
                done = np.all(done)
                past_action = action

                np_pred_image_seq = pred_img_seq.detach().to('cpu').numpy()
                for i in range(np_pred_image_seq.shape[1]):
                    image_buffer.append(np_pred_image_seq[:, i, :, :])
                # update pbar
                pbar.update(action.shape[1])

            # print the average and mean computation time
            #print('average computation time:', np.mean(comp_time_buffer))
            #print('std computation time:', np.std(comp_time_buffer))
            #print('max computation time:', np.max(comp_time_buffer))
            #print('num of steps:', len(comp_time_buffer))
            pbar.close()

            all_video_paths[this_global_slice] = env.render()[this_local_slice]
            all_rewards[this_global_slice] = env.call('get_attr', 'reward')[this_local_slice]
        # clear out video buffer
        _ = env.reset()

        # log
        max_rewards = collections.defaultdict(list)
        log_data = dict()

        # log images

        batched_data = [[] for _ in range(image_buffer[0].shape[0])]  # Prepare a list of lists for each batch
        for data in image_buffer:
            for batch_index in range(data.shape[0]):
                batched_data[batch_index].append(data[batch_index])
        videos = [np.stack(batch_images) for batch_images in batched_data]


        # save image in the input_image_list and rec_image_list
        # for each item, data is batch, height, width, channel
        # save all images in the batch, and name as input/rec_{batch_idx}_{list_idx}
        # using numpy imwrite
        '''
        imgpath = '/home/zhengtong/VILP/2pi_vis'
        for list_idx in range(len(input_image_list)):
            batch = input_image_list[list_idx]
            for batch_idx in range(batch.shape[0]):
                img = batch[batch_idx, :, :, :]
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                # save path: imgpath + /input/ + input_{batch_idx}_{list_idx}.png
                cv2.imwrite(imgpath +'/input' + f'/input_{batch_idx}_{list_idx}.png', img)
        for list_idx in range(len(rec_image_list)):
            batch = rec_image_list[list_idx]
            for batch_idx in range(batch.shape[0]):
                img = batch[batch_idx, :, :, :]
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                # save path: imgpath + /rec/ + rec_{batch_idx}_{list_idx}.png
                cv2.imwrite(imgpath +'/rec' + f'/rec_{batch_idx}_{list_idx}.png', img)
        '''

        # results reported in the paper are generated using the commented out line below
        # which will only report and average metrics from first n_envs initial condition and seeds
        # fortunately this won't invalidate our conclusion since
        # 1. This bug only affects the variance of metrics, not their mean
        # 2. All baseline methods are evaluated using the same code
        # to completely reproduce reported numbers, uncomment this line:
        # for i in range(len(self.env_fns)):
        # and comment out this line
        for i in range(n_inits):
            seed = self.env_seeds[i]
            prefix = self.env_prefixs[i]
            max_reward = np.max(all_rewards[i])
            max_rewards[prefix].append(max_reward)
            log_data[prefix+f'sim_max_reward_{seed}'] = max_reward

            # visualize sim
            video_path = all_video_paths[i]
            if video_path is not None:
                sim_video = wandb.Video(video_path)
                log_data[prefix+f'sim_video_{seed}'] = sim_video

        # log aggregate metrics
        for prefix, value in max_rewards.items():
            name = prefix+'mean_score'
            value = np.mean(value)
            log_data[name] = value


        for i, video in enumerate(videos):
            key = f"video_{i}"  # Unique key for each video
            # Change from (frames, c, height, width) to (frames, height, width, c)
            #video = np.moveaxis(video, 1, -1)
            video = (video*255).astype(np.uint8)  # Convert to 8-bit integer
            log_data[key] = wandb.Video(video)  # Convert PyTorch tensor to NumPy array
            # save the video to local
            #random_number = np.random.randint(0, 100000)
            #gobal_path = pathlib.Path(self.output_dir).joinpath(f'media/random_{random_number}')
            #video_path = f"video_{i}.mp4"
            #wandb.save(str(gobal_path.joinpath(video_path)))

    

        return log_data