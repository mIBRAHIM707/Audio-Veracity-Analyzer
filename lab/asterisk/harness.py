"""Disposable lab container for tests (backlog 6.1/6.2). Starts the locally built lab
image with a unique name and the label `ava-lab-test=1` (leftovers can be found with
`docker ps -a --filter label=ava-lab-test=1`), never pulls (`--pull never`), mounts a
temporary I/O folder at /lab/io (made writable for the container's asterisk user,
which Linux bind mounts otherwise refuse), waits for Asterisk to be fully booted, and
removes container and folder in `stop()`. Callers register `stop` as a cleanup BEFORE
calling `start()`, so a failure during start-up still removes everything.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

LABEL = 'ava-lab-test=1'


class LabError(RuntimeError):
    pass


class LabContainer:
    def __init__(self, image, *, network='none', publish=(), env=None, docker=None):
        self.docker = docker or shutil.which('docker')
        if self.docker is None:
            raise LabError('docker CLI not found')
        self.image, self.network, self.publish, self.env = image, network, tuple(publish), dict(env or {})
        self.name = f'ava-lab-test-{uuid.uuid4().hex[:10]}'
        self.io = None
        self.started = False

    def command(self, args, timeout=120, check=True, env=None):
        done = subprocess.run([self.docker, *args], capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **env} if env else None)
        if check and done.returncode != 0:
            raise LabError(f'docker {args[0]} failed ({done.returncode}): {done.stdout[-1500:]} {done.stderr[-1500:]}')
        return done

    def start(self, boot_timeout=90):
        self.io = tempfile.mkdtemp(prefix='ava-lab-io-')
        os.chmod(self.io, 0o777)  # the container's asterisk user writes recordings here
        args = ['run', '-d', '--rm', '--pull', 'never', '--label', LABEL, '--name', self.name,
                '--network', self.network, '-v', f'{self.io}:/lab/io']
        for mapping in self.publish:
            args += ['-p', mapping]
        for key in self.env:  # name only: the value travels in docker's environment, not its argv
            args += ['-e', key]
        self.started = True  # from here on stop() must try to remove the container
        self.command([*args, self.image], env=self.env)
        deadline = time.monotonic() + boot_timeout
        while True:
            done = self.command(['exec', self.name, 'asterisk', '-rx', 'core waitfullybooted'], timeout=60,
                                check=False)
            if done.returncode == 0 and 'fully booted' in done.stdout.lower():
                return self
            if 'is not running' in done.stderr or 'No such container' in done.stderr:
                raise LabError(f'lab container exited during start-up: {done.stderr[-300:]}')
            if time.monotonic() > deadline:
                raise LabError(f'Asterisk did not boot: {done.stdout[-500:]} {done.stderr[-500:]}')
            time.sleep(1)

    def port(self, container_port):
        """Host address of a published port, e.g. '127.0.0.1:49153'."""
        done = self.command(['port', self.name, container_port])
        return done.stdout.strip().splitlines()[0]

    def stop(self):
        try:
            if self.started:
                try:
                    stopped = self.command(['stop', '-t', '5', self.name], timeout=60, check=False)
                    if stopped.returncode != 0:
                        self.command(['rm', '-f', self.name], timeout=60, check=False)
                except subprocess.TimeoutExpired:
                    self.command(['rm', '-f', self.name], timeout=60, check=False)
        finally:
            if self.io:
                shutil.rmtree(self.io, ignore_errors=True)

    def path(self, name):
        return Path(self.io, name)
