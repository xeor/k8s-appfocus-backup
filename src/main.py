import os
import time
import logging

import kopf

from kubernetes import config, client
from kubernetes.stream import stream

import prometheus_client as prometheus

prometheus.start_http_server(9090)
m_exec_summary = prometheus.Summary(
    "karb_exec_seconds",
    "Time spent executing successful backup command",
    [
        "friendly_name",
        "namespace",
        "pod_name",
        "container_name",
        "backup_name",
        "backup_schedule",
    ],
)
PROMETHEUS_DISABLE_CREATED_SERIES = True
m_exec_counter = prometheus.Counter(
    "karb_exec_total",
    "Total exec requests",
    [
        "friendly_name",
        "status",
        "namespace",
        "pod_name",
        "container_name",
        "backup_name",
        "backup_schedule",
    ],
)

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


def exec_backup_command_in_pod(
    namespace,
    pod_name,
    container_name,
    command,
    shell=None,
    backup_name="",
    backup_schedule="",
):
    if not command:
        raise kopf.TemporaryError(f"No command specified", delay=60)

    exec_command = get_exec_command(shell, command)

    # logging.info(f"exec: {namespace=}, {pod_name=}, {container_name=}, {exec_command=}")

    friendly_name = f"{namespace}/{pod_name} ({backup_name})"

    start_time = time.time()
    try:
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
    except Exception as err:
        m_exec_counter.labels(
            friendly_name,
            "failed",
            namespace,
            pod_name,
            container_name,
            backup_name,
            backup_schedule,
        ).inc()
        raise kopf.TemporaryError(f"Error during exec: {err}", delay=60)

    # Check if we can get returncode of the command as well
    duration = time.time() - start_time
    m_exec_summary.labels(
        friendly_name, namespace, pod_name, container_name, backup_name, backup_schedule
    ).observe(duration)

    m_exec_counter.labels(
        friendly_name,
        "success",
        namespace,
        pod_name,
        container_name,
        backup_name,
        backup_schedule,
    ).inc()
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
    settings.execution.max_workers = (
        1000  # Should be configurable? Or use async instead
    )

    # Want to make something ourself here. Since daemons are always running and holding
    # the finalizer
    # settings.persistence.finalizer = 'my-operator.example.com/kopf-finalizer'

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


@kopf.daemon("pods.v1", annotations={"karb.boa.nu/backup-schedule": kopf.PRESENT})
def run_backups(stopped, name, namespace, spec, annotations, **kwargs):

    while not stopped and not is_pod_ready(namespace, name):
        logging.info(f"Pod in {namespace}/{name} not ready yet...")
        stopped.wait(5)

    schedule = int(annotations["karb.boa.nu/backup-schedule"])
    logging.info(
        f"Pod in {namespace}/{name} ready. Will backup every {schedule} seconds"
    )

    container = get_main_container(
        spec, name=annotations.get("karb.boa.nu/container-name")
    )
    while not stopped:
        ret = exec_backup_command_in_pod(
            namespace,
            name,
            container["name"],
            annotations.get("karb.boa.nu/backup-exec"),
            shell=annotations.get("karb.boa.nu/backup-exec-shell"),
            backup_name=annotations.get("karb.boa.nu/backup-name", "default"),
            backup_schedule=schedule,
        )
        logging.info(
            f"Executed backup-exec-shell command in {namespace}/{name} [{container['name']}] with return {ret}"
        )

        stopped.wait(schedule)


@kopf.on.mutate("pods.v1", annotations={"karb.boa.nu/backup-schedule": kopf.PRESENT})
def mutate(body, annotations, patch, **kwargs):
    spec = body["spec"]
    containers = spec.get("containers", [])
    init_containers = spec.get("initContainers", [])
    volumes = spec.get("volumes", [])

    # Add backup volume
    if "karb-backup-volume" not in [i["name"] for i in volumes]:
        backupname = annotations.get("karb.boa.nu/backup-name", "default")
        nfs_root_path = os.environ["NFS_ROOT_PATH"]
        os.makedirs(f"/karb-data-root/{backupname}", exist_ok=True)
        volumes.append(
            {
                "name": "karb-backup-volume",
                "nfs": {
                    "server": os.environ["NFS_SERVER"],
                    "path": f"{nfs_root_path}/{backupname}",
                },
            }
        )
        patch.spec["volumes"] = volumes

    # This is the container running the app
    container = get_main_container(
        spec, name=annotations.get("karb.boa.nu/container-name")
    )

    # Add karb-backup-volume if it is missing
    if "karb-backup-volume" not in [i["name"] for i in container["volumeMounts"]]:
        container["volumeMounts"].append(
            {
                "name": "karb-backup-volume",
                "readOnly": False,
                "mountPath": "/karb-data",
            }
        )
    patch.spec["containers"] = containers

    # Add init-container if missing
    if "karb-restorer" not in [i["name"] for i in init_containers]:
        init_container = container.copy()
        init_container["name"] = "karb-restorer"

        # Need to replace "command" here
        init_container["command"] = get_exec_command(
            annotations.get("karb.boa.nu/restore-exec-shell"),
            annotations.get("karb.boa.nu/restore-exec"),
        )

        init_containers.append(init_container)
        patch.spec["initContainers"] = init_containers

    return {}
