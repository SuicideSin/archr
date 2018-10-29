import subprocess
import contextlib
import tempfile
import logging
import tarfile
import docker
import json
import os
import io

l = logging.getLogger("archr.target.docker_target")

from . import Target

class DockerImageTarget(Target):
    """
    Describes a target in the form of a Docker image.
    """

    def __init__(
        self, image_name,
        pull=False, target_args=None, target_path=None, target_env=None,
        **kwargs
                 #target_port=None,
                 #target_arch=None,
    ):
        super(DockerImageTarget, self).__init__(**kwargs)

        self._client = docker.client.from_env()
        self.image_id = image_name

        if pull:
            self._client.images.pull(self.image_id)

        self.target_args = target_args
        self.target_path = target_path
        self.target_env = target_env
        self.image = None
        self.container = None
        self.subprocess = None
        self.project = None

    #
    # Lifecycle
    #

    def build(self, *args, **kwargs):
        self.image = self._client.images.get(self.image_id)
        self.target_args = (
            self.target_args or
            (self.image.attrs['Config']['Entrypoint'] or [ ]) + (self.image.attrs['Config']['Cmd'] or [ ])
        )
        self.target_env = self.target_env or self.image.attrs['Config']['Env']
        self.target_path = self.target_path or self.target_args[0]
        return self

    def remove(self):
        if self.container:
            self.container.remove(force=True)

    def start(self):
        self.container = self._client.containers.run(
            self.image,
            entrypoint=['/bin/sh'], command=[], environment=self.target_env,
            detach=True, auto_remove=True,
            stdin_open=True, stdout=True, stderr=True,
            privileged=True, security_opt=["seccomp=unconfined"], #for now, hopefully...
            #network_mode='bridge', ports={11111:11111, self.target_port:self.target_port}
        )
        try: os.makedirs(self.local_path)
        except OSError: pass
        os.system("sudo mount -o bind %s %s" % (self._merged_path, self.local_path))
        return self

    def stop(self):
        if self.container:
            self.container.kill()
        os.system("sudo umount %s" % self.local_path)
        os.rmdir(self.local_path)

    #
    # File access
    #

    @property
    def _merged_path(self):
        return self.container.attrs['GraphDriver']['Data']['MergedDir']

    @property
    def local_path(self):
        return "/tmp/archr_mounts/%s" % self.container.id

    def resolve_local_path(self, path):
        if not path.startswith(self.local_path):
            path = os.path.join(self.local_path, path.lstrip("/"))
        realpath = os.path.realpath(path)
        if not realpath.startswith(self.local_path):
            realpath = os.path.join(self.local_path, realpath.lstrip("/"))
        return realpath

    def inject_paths(self, files):
        """
        Injects files or directories into the target.

        :param list files: A list of (src,dst) tuples.
        """
        f = io.BytesIO()
        t = tarfile.open(fileobj=f, mode='w')
        for src,dst in files:
            t.add(src, arcname=dst)
        t.close()
        f.seek(0)
        b = f.read()
        self.container.put_archive("/", b)

    def inject_path(self, src, dst=None):
        """
        Injects a file or directory into the target.

        :param str src: the source path (on the host)
        :param str dst: the dst path (on the target)
        """
        self.inject_paths([(src, dst)])

    def inject_tarball(self, tarball_path, target_path):
        """
        Extracts a tarball into the target.

        :param str tarball_path: The content of the tarball.
        :param str target_path: The path to extract to.
        """
        with open(tarball_path) as t:
            b = t.read()
        self.container.put_archive(target_path, b)

    def retrieve_tarball_contents(self, target_path):
        """
        Retrieves files from the target in the form of tarball contents.

        :param str target_path: The path to retrieve.
        """
        stream, _ = self.container.get_archive(target_path)
        return b''.join(stream)

    def retrieve_file_contents(self, target_path):
        """
        Retrieves the contents of a file from the target.

        :param str target_path: The path to retrieve.
        :returns bytes: the contents of the file
        """
        f = io.BytesIO()
        f.write(self.retrieve_tarball_contents(target_path))
        f.seek(0)
        t = tarfile.open(fileobj=f, mode='r')
        return t.extractfile(os.path.basename(target_path)).read()

    def retrieve_path(self, target_path, local_path):
        """
        Retrieves a path on the target to a path remotely.

        :param str target_path: The path to retrieve.
        :param str local_path: The path to put it locally.
        """
        f = io.BytesIO()
        f.write(self.retrieve_tarball_contents(target_path))
        f.seek(0)
        t = tarfile.open(fileobj=f, mode='r')
        t.extractall(os.path.join(local_path, os.path.dirname(target_path).lstrip("/")), members=[m for m in t.getmembers() if m.path.startswith(target_path.lstrip("/"))])

    @contextlib.contextmanager
    def retrieval_context(self, target_path, local_thing=None):
        """
        This is a context manager that retrieves a file from the target upon exiting.

        :param str target_path: the path on the target to retrieve
        :param local_thing: Can be a file path (str) or a write()able object (where the file will be written upon retrieval), or None, in which case a temporary file will be yielded.
        """

        with contextlib.ExitStack() as stack:
            if type(local_thing) in (str, bytes):
                to_yield = local_thing
                local_file = stack.enter_context(open(local_thing, "wb"))
            elif local_thing is None:
                to_yield = tempfile.mktemp()
                local_file = stack.enter_context(open(to_yield, "wb"))
            elif hasattr(local_thing, "write"):
                to_yield = local_thing
                local_file = local_thing
            else:
                raise ValueError("local_thing argument to retrieval_context() must be a str, a write()able object, or None")

            try:
                yield to_yield
            finally:
                local_file.write(self.retrieve_file_contents(target_path))

    #
    # Info access
    #

    @property
    def ipv4_address(self):
        if self.container is None:
            return None
        return json.loads(
            subprocess.Popen(["docker", "inspect", self.container.id], stdout=subprocess.PIPE).communicate()[0].decode()
        )[0]['NetworkSettings']['IPAddress']

    @property
    def tcp_ports(self):
        try:
            return [ int(k.split('/')[0]) for k in self.image.attrs['ContainerConfig']['ExposedPorts'].keys() if 'tcp' in k ]
        except KeyError:
            return [ ]

    @property
    def udp_ports(self):
        try:
            return [ int(k.split('/')[0]) for k in self.image.attrs['ContainerConfig']['ExposedPorts'].keys() if 'udp' in k ]
        except KeyError:
            return [ ]

    #
    # Execution
    #

    def run_command(
        self, args=None, args_prefix=None, args_suffix=None, aslr=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ): #pylint:disable=arguments-differ
        assert self.container is not None

        command_args = args or self.target_args
        if args_prefix:
            command_args = args_prefix + command_args
        if args_suffix:
            command_args = command_args + args_suffix
        if not aslr:
            command_args = ['setarch', 'x86_64', '-R'] + command_args

        docker_args = [ "docker", "exec", "-i" ]
        for e in self.target_env:
            docker_args += [ "-e", e ]
        docker_args.append(self.container.id)

        return subprocess.Popen(
            docker_args + command_args,
            stdin=stdin, stdout=stdout, stderr=stderr, bufsize=0
        )

    @contextlib.contextmanager
    def run_context(self, *args, **kwargs):
        """
        A context around run_command. Yields a subprocess.
        """

        p = self.run_command(*args, **kwargs)
        try:
            yield p
            p.wait()
        finally:
            # TODO: probably insufficient
            p.terminate()
