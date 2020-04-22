import os
import time
import signal
import threading

from popper import utils as pu
from popper.cli import log as log
from popper.runner_host import HostRunner
from popper.runner_host import DockerRunner as HostDockerRunner


class SlurmRunner(HostRunner):
    def __init__(self, **kw):
        super(SlurmRunner, self).__init__(**kw)
        self._spawned_jobs = set()

    def __exit__(self, exc_type, exc, traceback):
        self._spawned_jobs = set()

    def _tail_output(self, out_file):
        self._out_stream_pid = set()
        _, ecode, _ = HostRunner._exec_cmd(["tail", "-f", out_file],
                                           pids=self._out_stream_pid)
        return ecode

    def _start_out_stream(self, out_file):
        self._out_stream_thread = threading.Thread(
            target=self._tail_output, args=(out_file,))
        self._out_stream_thread.start()
        time.sleep(2)

    def _stop_out_stream(self):
        _out_stream_pid = list(self._out_stream_pid)[0]
        try:
            os.kill(_out_stream_pid, 0)
            os.kill(_out_stream_pid, signal.SIGKILL)
        except ProcessLookupError:
            log.warning('Tail process was killed by some other process.')
        self._out_stream_thread.join()

    def _submit_batch_job(self, cmd, step):
        job_name = pu.sanitized_name(step['name'], self._config.wid)
        temp_dir = "/tmp/popper/slurm/"
        os.makedirs(temp_dir, exist_ok=True)

        job_script = os.path.join(temp_dir, f'{job_name}.sh')
        out_file = os.path.join(temp_dir, f'{job_name}.out')

        # create/truncate log
        with open(out_file, 'w'):
            pass

        with open(job_script, 'w') as f:
            f.write('#!/bin/bash\n')
            f.write('\n'.join(cmd))

        sbatch_cmd = f'sbatch --wait --job-name {job_name} --output {out_file}'
        sbatch_cmd = sbatch_cmd.split()

        for k, v in self._config.resman_opts.get(step['name'], {}).items():
            sbatch_cmd.append(pu.key_value_to_flag(k, v))

        sbatch_cmd.append(job_script)

        log.info(f'[{step["name"]}] {" ".join(sbatch_cmd)}')

        if self._config.dry_run:
            return 0

        self._spawned_jobs.add(job_name)

        # start a tail (background) process on the output file
        self._start_out_stream(out_file)

        # submit the job and wait
        _, ecode, output = HostRunner._exec_cmd(sbatch_cmd, logging=False)

        # kill the tail process
        self._stop_out_stream()

        self._spawned_jobs.remove(job_name)

        return ecode

    def stop_running_tasks(self):
        for job_name in self._spawned_jobs:
            log.info(f'Cancelling job {job_name}')
            _, ecode, _ = HostRunner._exec_cmd(['scancel', '--name', job_name])
            if ecode != 0:
                log.warning(f'Failed to cancel the job {job_name}.')


class DockerRunner(SlurmRunner, HostDockerRunner):

    def __init__(self, **kw):
        super(DockerRunner, self).__init__(init_docker_client=False, **kw)

    def __exit__(self, exc_type, exc, traceback):
        pass

    def run(self, step):
        """Execute the given step via slurm in the docker engine."""
        cid = pu.sanitized_name(step['name'], self._config.wid)
        cmd = []

        build, img, tag, dockerfile = self._get_build_info(step)

        cmd.append(f'docker rm -f {cid} || true')

        if build:
            cmd.append(f'docker build -t {img}:{tag} {dockerfile}')
        elif not self._config.skip_pull and not step.get('skip_pull', False):
            cmd.append(f'docker pull {img}:{tag}')

        cmd.append(self._create_cmd(step, f'{img}:{tag}', cid))
        cmd.append(f'docker start --attach {cid}')

        if self._config.dry_run:
            return 0

        self._spawned_containers.add(cid)
        ecode = self._submit_batch_job(cmd, step)
        self._spawned_containers.remove(cid)
        return ecode

    def _create_cmd(self, step, img, cid):
        container_args = self._get_container_kwargs(step, img, cid)
        container_args.pop('detach')
        cmd = ['docker create']
        cmd.append(f"--name {container_args.pop('name')}")
        cmd.append(f"--workdir {container_args.pop('working_dir')}")

        entrypoint = container_args.pop('entrypoint', None)
        if entrypoint:
            cmd.append(f"--entrypoint {' '.join(entrypoint)}")

        # append volume and environment flags
        for vol in container_args.pop('volumes'):
            cmd.append(f'-v {vol}')
        for env_key, env_val in container_args.pop('environment').items():
            cmd.append(f'-e {env_key}={env_val}')

        command = ' '.join(container_args.pop('command', []))
        image = container_args.pop('image')

        # anything else is treated as a flag
        for k, v in container_args.items():
            cmd.append(pu.key_value_to_flag(k, v))

        # append the image and the commands
        cmd.append(f'{image} {command}')

        return ' '.join(cmd)