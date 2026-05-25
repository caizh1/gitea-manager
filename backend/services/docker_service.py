import logging
import docker

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def local_exec(container_name, cmd, user=None, workdir=None):
    client = _get_client()
    container = client.containers.get(container_name)
    exit_code, output = container.exec_run(cmd, user=user, workdir=workdir)
    return exit_code, output.decode('utf-8', errors='replace')


def local_cp_from(container_name, container_path, host_path):
    client = _get_client()
    container = client.containers.get(container_name)
    bits, stat = container.get_archive(container_path)
    with open(host_path, 'wb') as f:
        for chunk in bits:
            f.write(chunk)


def local_cp_to(host_path, container_name, container_path):
    import tarfile
    import io
    import os

    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode='w') as tar:
        tar.add(host_path, arcname=os.path.basename(host_path))
    tar_stream.seek(0)

    client = _get_client()
    container = client.containers.get(container_name)
    container.put_archive(os.path.dirname(container_path), tar_stream)
