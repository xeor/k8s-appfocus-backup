import os
import logging

import kopf

from kubernetes import config, client
from kubernetes.stream import stream

if "DEV" in os.environ:
    config.load_kube_config()
else:
    config.load_incluster_config()

api = client.CoreV1Api()


def is_pod_ready(namespace, pod_name):
    try:
        pod = api.read_namespaced_pod(namespace=namespace, name=pod_name)
        if pod.status.conditions:
            for condition in pod.status.conditions:
                if condition.type == "Ready" and condition.status == "True":
                    return True
        return False
    except client.rest.ApiException as e:
        print(f"API exception when reading pod status: {e}")
        return False


def get_exec_command(shell, command):
    if shell:
        shell = shell.split()
    else:
        shell = ["/bin/sh", "-c"]

    return shell + [command]


def exec_command_in_pod(namespace, pod_name, container_name, command, shell=None):
    if not command:
        raise kopf.TemporaryError(f"No command specified", delay=60)

    exec_command = get_exec_command(shell, command)

    # logging.info(f"exec: {namespace=}, {pod_name=}, {container_name=}, {exec_command=}")

    resp = stream(
        api.connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        command=exec_command,
        container=container_name,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )

    return resp


def get_main_container(spec, name):
    if len(spec["containers"]) == 1:
        return spec["containers"][0]

    for i in spec["containers"]:
        if i["name"] == name:
            return i

    raise kopf.TemporaryError(f"No container named {name} found.", delay=60)


@kopf.on.login()
def login(**kwargs):
    token_file = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    if os.path.isfile(token_file):
        logging.info(
            f"Found serviceaccount token file at {token_file}. Login via service-account"
        )
        return kopf.login_with_service_account(**kwargs)
    logging.info("Login via kubeconfig, no token-file found")
    return kopf.login_with_kubeconfig(**kwargs)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    settings.posting.level = logging.INFO

    config = {
        "port": int(os.environ.get("webhook_port", "8443")),
        "addr": "0.0.0.0",
        "cafile": "/etc/certs/ca.crt",
        "certfile": "/etc/certs/tls.crt",
        "pkeyfile": "/etc/certs/tls.key",
    }
    if "webhook_host" in os.environ:
        config["host"] = os.environ["webhook_host"]
    settings.admission.server = kopf.WebhookServer(**config)


@kopf.daemon("pods.v1", annotations={"kab.boa.nu/backup-schedule": kopf.PRESENT})
def run_backups(stopped, name, namespace, spec, annotations, **kwargs):

    while not stopped and not is_pod_ready(namespace, name):
        logging.info(f"Pod in {namespace}/{name} not ready yet...")
        stopped.wait(5)

    schedule = int(annotations["kab.boa.nu/backup-schedule"])
    logging.info(
        f"Pod in {namespace}/{name} ready. Will backup every {schedule} seconds"
    )

    container = get_main_container(
        spec, name=annotations.get("kab.boa.nu/container-name")
    )
    while not stopped:
        ret = exec_command_in_pod(
            namespace,
            name,
            container["name"],
            annotations.get("kab.boa.nu/backup-exec"),
            shell=annotations.get("kab.boa.nu/backup-exec-shell"),
        )
        logging.info(
            f"Executed backup-exec-shell command in {namespace}/{name} [{container['name']}] with return {ret}"
        )

        stopped.wait(schedule)


@kopf.on.mutate("pods.v1", annotations={"kab.boa.nu/backup-schedule": kopf.PRESENT})
def mutate(body, annotations, patch, **kwargs):
    spec = body["spec"]
    containers = spec.get("containers", [])
    init_containers = spec.get("initContainers", [])
    volumes = spec.get("volumes", [])

    # Add backup volume
    if "backup-volume" not in [i["name"] for i in volumes]:
        backupname = annotations.get("kab.boa.nu/backup-name", "default")
        nfs_root_path = os.environ["NFS_ROOT_PATH"]
        volumes.append(
            {
                "name": "backup-volume",
                "nfs": {
                    "server": os.environ["NFS_SERVER"],
                    "path": f"{nfs_root_path}/{backupname}",
                },
            }
        )
        patch.spec["volumes"] = volumes

    # This is the container running the app
    container = get_main_container(
        spec, name=annotations.get("kab.boa.nu/container-name")
    )

    # Add backup-volume if it is missing
    if "backup-volume" not in [i["name"] for i in container["volumeMounts"]]:
        container["volumeMounts"].append(
            {
                "name": "backup-volume",
                "readOnly": False,
                "mountPath": "/kab-backup",
            }
        )
    patch.spec["containers"] = containers

    # Add init-container if missing
    if "kab-restorer" not in [i["name"] for i in init_containers]:
        init_container = container.copy()
        init_container["name"] = "kab-restorer"

        # Need to replace "command" here
        init_container["command"] = get_exec_command(
            annotations.get("kab.boa.nu/restore-exec-shell"),
            annotations.get("kab.boa.nu/restore-exec"),
        )

        init_containers.append(init_container)
        patch.spec["initContainers"] = init_containers

    return {}
