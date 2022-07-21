import io
import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional, Union

import paramiko
from boto3.session import Session
from scp import SCPClient

import botocore
from nanoid import generate


logger = logging.getLogger(__name__)
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)


class RemoteRunner:
    """
    Set up cloud infrastructure and run scripts.
    """

    def __init__(
        self,
        run_name: Optional[str] = f"rm-runner-{generate('abcdefghijklm', 4)}",
        instance_type: str = "t3.micro",
        container: str = "vault.habana.ai/gaudi-docker/1.4.1/ubuntu20.04/habanalabs/pytorch-installer-1.10.2:1.4.1-11 hl-smi",
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        session_token: Optional[str] = None,
        region: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> None:
        self.session = Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            aws_session_token=session_token,
            region_name=region,
            profile_name=profile,
        )
        self.ec2_client = self.session.client("ec2")
        self.ec2_resource = self.session.resource("ec2")
        self.run_name = run_name
        self.instance_type = instance_type
        self.container = container
        self.ami_id = self._get_ami_from_instance_type(instance_type)

    def _start(self) -> None:
        key = self._create_ec2_key_pair()
        sg_id = self._create_ec2_security_group_with_ssh_ingress()
        instance_id = self._run_ec2_instance(
            ami_id=self.ami_id, instance_type=self.instance_type, key_name=self.run_name, sg_id=sg_id
        )
        self.instance = self.ec2_resource.Instance(id=instance_id)
        logger.info(f"Waiting for instance to be ready...")
        self.instance.wait_until_running()
        public_dns = self.instance.public_dns_name
        logger.info(f"Instance is ready. Public DNS: {public_dns}")
        self.ssh_client = self._setup_ssh_connection(key=key, instance_dns=public_dns)

    def _exec_command(
        self, command: Optional[str], source_dir: Union[Path, str] = None, args: List[str] = None
    ) -> str:
        # read script and move to remote
        exec_source_dir = source_dir if source_dir else "/home/ubuntu"
        full_command = [
            "docker run",
            "--runtime=habana -e HABANA_VISIBLE_DEVICES=all -e OMPI_MCA_btl_vader_single_copy_mechanism=none",
            "--entrypoint /bin/bash",
            "--cap-add=sys_nice --net=host --ipc=host",
            f"-v {exec_source_dir}:/home/ubuntu/rm-runner --workdir=/home/ubuntu/rm-runner",
            f"{self.container}",
            f"{command}",
        ]
        logger.info(f"Executing: {full_command}")

        stdin, stdout, stderr = self.ssh_client.exec_command(
            " ".join(full_command),
            get_pty=True,
        )
        while True:
            out = stdout.channel.recv(1024)
            if not out:
                break
            sys.stdout.write(out.decode())
            sys.stdout.flush()

    def _upload_data(self, source_dir: Union[Path, str]) -> None:
        remote_path = "/home/ubuntu/test"
        with SCPClient(self.ssh_clientssh.get_transport()) as scp:
            scp.put(source_dir, recursive=True, remote_path="/home/ubuntu/test")
        return remote_path

    def _stop(self) -> None:
        # termiante ec2 instances
        logger.info(f"Terminating instance: {self.instance.id}")
        self.instance.terminate()
        # wait for ec2 instances to be terminated
        self.instance.wait_until_terminated()
        # delete sg
        logger.info(f"Deleting security group: {self.run_name}")
        self.ec2_client.delete_security_group(GroupName=self.run_name)
        # delete key
        logger.info(f"Deleting key: {self.run_name}")
        self.ec2_client.delete_key_pair(KeyName=self.run_name)

    def launch(self, command: Optional[str], source_dir: Union[Path, str] = None) -> None:
        start_time = time.time()
        # create ec2
        self._start()
        # launch
        try:
            if source_dir:
                source_dir = self._upload_data(source_dir)
            self._exec_command(source_dir=source_dir, command=command)
        except Exception as e:
            logger.error(e)
            self._stop()
            raise e
        # stop
        self._stop()
        logger.info(f"Total time: {round(time.time() - start_time)}s")

    def _create_ec2_key_pair(self) -> str:
        try:
            key = self.ec2_client.create_key_pair(KeyName=self.run_name)["KeyMaterial"]
        except Exception as e:
            if "Duplicate" in str(e):
                self.ec2_client.delete_key_pair(KeyName=self.run_name)
                key = self.ec2_client2.create_key_pair(KeyName=self.run_name)["KeyMaterial"]
            else:
                raise e
        logger.info(f"Created key pair: {self.run_name}")
        return key

    def _create_ec2_security_group_with_ssh_ingress(self) -> str:
        try:
            sg_id = self.ec2_client.create_security_group(
                GroupName=self.run_name, Description="rm-runner only allow SSH traffic"
            )["GroupId"]
        except Exception as e:
            if "Duplicate" in str(e):
                self.ec2_client.delete_security_group(GroupName=self.run_name)
                sg_id = self.ec2_client.create_security_group(
                    GroupName=self.run_name, Description="rm-runner only allow SSH traffic"
                )["GroupId"]
            else:
                raise e
        finally:
            self.ec2_client.authorize_security_group_ingress(
                GroupId=sg_id, IpProtocol="tcp", CidrIp="0.0.0.0/0", FromPort=22, ToPort=22
            )
        logger.info(f"Created security group: {self.run_name}")
        return sg_id

    def _run_ec2_instance(
        self, ami_id="ami-06d20e48ee8d06029", instance_type="t3.micro", sg_id=None, key_name=None, volume_size=150
    ):
        instance = self.ec2_client.run_instances(
            BlockDeviceMappings=[
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {"DeleteOnTermination": True, "VolumeSize": volume_size, "VolumeType": "gp2"},
                },
            ],
            ImageId=ami_id,
            InstanceType=instance_type,
            MaxCount=1,
            MinCount=1,
            SecurityGroupIds=[sg_id],
            KeyName=key_name,
            # tag for name
        )
        logger.info(f"Launched instance: {instance['Instances'][0]['InstanceId']}")
        return instance["Instances"][0]["InstanceId"]

    def _setup_ssh_connection(self, key: str, instance_dns: str) -> paramiko.SSHClient:
        #         @staticmethod
        # @contextmanager
        # def _connect_ssh_context(host, username, password, load_host_keys=True):
        #     try:
        #         ssh = paramiko.SSHClient()
        #         if load_host_keys:
        #             ssh.load_host_keys(os.path.expanduser("~/.ssh/known_hosts"))
        #         ssh.connect(host, username=username, password=password)
        #         yield ssh
        #     finally:
        #         ssh.close()

        key = paramiko.RSAKey.from_private_key(io.StringIO(key))
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        t0 = 0
        while t0 < 10:
            try:
                logger.info(f"Setting up ssh connection...")
                ssh.connect(instance_dns, username="ubuntu", pkey=key)
                break
            except Exception:
                t0 += 1
                time.sleep(5)
        return ssh

    def _get_ami_from_instance_type(self, instance_type):
        if "dl1" in instance_type:
            return "ami-06d20e48ee8d06029"
        else:
            raise NotImplementedError("only habana support")