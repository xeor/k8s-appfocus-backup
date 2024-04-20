# kab - k8s appfocus backup

## Intro

There are many backup-solutions for kubernetes, but many of them are overly complicated.
Don't use this solution if you want something rock solid. This backup solution solves the whole backup problem a little different, mostly tailered for small clusters and home-labs.

The application container itself have access to the backups. It's trivial for someone with access inside the container to also delete the backup. Make sure this is not your only backup!

* Why
  * You want to use the application to backup itself
  * You don't want to depend on s3 or similar services to do restore
  * You want an easy way to restore the service, also outside kubernetes

* Why not
  * You want something battletested
  * You need backup of more kubernetes contexts, not only the data

## How it works

kab is an operator that looks for pods with special annotations.
* It will inject a backup-volume (from nfs) into your container on `/kab-backup`
* It will execute a command (or tiny shell script) defined in the annotations on a schedule. You are supposed to do the backup here and put it on `/kab-backup`
* When you start the pod, we will
  * Create a duplicate of your main container as an init container. We will run the restore-exec annotation in it
  * You are supposed to check if restore is needed, if it is, restore. You also have the `/kab-backup` volume. Main container won't start before we are done.
* `/kab-backup` will be mounted based on the backup-name defined. No pod-id or stuff like that. This is by design.

Using this pattern, we can run the apps own backup command (like pgdump, somecli backup, ...) on a schedule. Then, when the container starts, you can check if backup is needed and it will automatically restore.

Since the backup volume is defined based on a name you choose, you can very easiely reinstall the whole cluster and the restores will be done automatically for you because your restore scripts have access to your newest backup, runs in the init container, and have the content of your app and it's volumes.

## Spec

Here is a heavely commented example-pod

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: example
  annotations:

    # Will be the subfolder of your backup volume. If you have configured
    # the operator to use path `/data/backup`, it will become `/data/backup/test`
    # in this case. Default is "default" if this annotation is left out.
    kab.boa.nu/backup-name: "test"

    # What container to run backup commands in and base the init container on.
    # It's important that this is the app-container itself. If left out
    # and there is only one container, we will use that. If there are more than one
    # and this is not defined, you will get an error.
    kab.boa.nu/container-name: "my-container"

    # Currently only how many seconds we should sleep before doing the next backup.
    kab.boa.nu/backup-schedule: "100"

    # What shell should we run the backup command in. If you want to run a shell-script
    # you should define it as "/bin/sh -c", which is the default if undefined
    kab.boa.nu/backup-exec-shell: "/bin/sh -c"

    # Script or command to run when doing backup. This command will be executed
    # inside the running app-container. You should put your backup in /kab-backup
    kab.boa.nu/backup-exec: |
      date >> /kab-backup/test

    # Same concept as kab.boa.nu/backup-exec-shell
    kab.boa.nu/restore-exec-shell: "/bin/sh -c"

    # Command to run in the init-container when pod is starting.
    # It is important that you check if you really want to do a restore before
    # you do the restore. That, and the restore itself is done here.
    kab.boa.nu/restore-exec: |
      if [ -f "/kab-backup/test"]; then
        cp /kab-backup/test /etc/test
        echo restored
        exit 0
      fi
      echo "not restoring"

spec:
  containers:
    - name: my-container
      image: busybox
      command: ["sh", "-c", "echo 'starting to sleep' && sleep 3600"]

  # If you want a custom restorer, instead of a "clone" of your main container
  # you can create an initContainer with the name "kab-restorer". If that exists
  # we will use that instead of creating it.
  # initContainers

```
