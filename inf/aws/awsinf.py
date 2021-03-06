#! /user/bin python

import time
from autopilot.common import logger
from autopilot.common import exception
from autopilot.common.utils import Dct
from autopilot.common.asyncpool import taskpool
from autopilot.inf.inf import Inf, InfResponseContext
from autopilot.inf.aws import awsutils
from autopilot.inf.aws import awsexception


class AWSInfResponseContext(InfResponseContext):
    def __init__(self, spec):
        """
        Response context base class for tracking AWS resquests
        """
        InfResponseContext.__init__(self, spec)


class AwsInfProvisionResponseContext(AWSInfResponseContext):
    """
    Response Context object to track AWS instance provisioning requests
    """
    def __init__(self, spec, reservation=None):
        AWSInfResponseContext.__init__(self, spec=spec)
        self.reservation = reservation
        self.log = logger.get_logger("AwsInfProvisionResponseContext")

    def all_instances_in_state(self, state="running"):
        for instance in self.reservation.instances:
            instance.update()
            # if any instance is pending return True
            self.log.debug("Instance {0} in state {1}".format(instance.id, instance.state))
            if instance.state != state:
                return False
        return True

    def close_on_instances_ready(self, timeout=180, interval=10):
        """
        Close when all instances are running
        """
        if self.yield_until_instances_in_state(state="running", timeout=timeout, interval=interval):
            self.log.debug("Instances in state: running")
            self.close()
            return True
        else:
            self.log.warning("Timedout waiting for instances to be in running state. timeout: {0}. Interval: {1}"
                             .format(timeout, interval))
            self.close(new_errors=[exception.AWSInstanceProvisionTimeout(self.reservation.instances)])
            return False

    def yield_until_instances_in_state(self, state="running", timeout=180, interval=10):
        """
        Yield until all instances are in a specified state
        """
        self.log.debug("yield_until_instances_in_state: {0} {1}".format(timeout, interval))
        max_tries = timeout/interval
        attempt = 0
        while attempt < max_tries:
            self.log.debug("yield_until_instances_in_state. Try count {0} out of {1}".format(attempt, max_tries))
            attempt += 1
            if not self.all_instances_in_state(state=state):
                taskpool.doyield(seconds=interval)
            else:
                return True
        return False


class AWSInf(Inf):
    """
    AWS Infrastructure management functions

    All aws api calls are pumped through gevent and should yield if we have monkey patched
    somewhere along the call chain.
    """
    def __init__(self, aws_access_key=None, aws_secret_key=None):
        Inf.__init__(self)
        self.log = logger.get_logger("AWSInf")
        self.aws_access_key = aws_access_key
        self.aws_secret_key = aws_secret_key
        self.vpc_conn = self._get_vpc()
        self.ec2_conn = self._get_ec2()

    def serialize(self):
        return dict(target="aws", properties=dict(aws_access_key="", aws_secret_key=""))

    def init_domain(self, domain_spec):
        """
        Create a private cluster environment based on the spec.
        At minimum this will create:
        1. VPC
        2. Internet gateway with internet routing enabled and attach to the VPC
        """
        rc = AWSInfResponseContext(spec=domain_spec)
        domain = domain_spec.get("domain")
        self.log.info("Init Domain for: {0}".format(domain))
        # check if we already have a vpc_id in the domain_Spec.
        # todo: check with aws if the vpc is actually present
        if not domain_spec.get('vpc_id'):
            try:
                self.log.info("Creating VPC for domain: {0}".format(domain))
                # create a vpc and a default internet gateway
                data = self.vpc_conn.create_vpc(cidr_block=Dct.get(domain_spec, "cidr", "10.0.0.0/16"),
                                                tags=dict(Name=domain))
                domain_spec["vpc_id"] = data["vpc"].id
                domain_spec["internet_gateway_id"] = data["internet_gateway"].id
            except Exception, e:
                self.log.error("Error initializing domain", exc_info=e)
                rc.errors.extend(e)

        # return updated spec
        rc.close(new_spec=domain_spec)
        return rc

    def init_stack(self, domain_spec, stack_spec):
        """
        Create subnets attached to the VPC and add new route table. Route subnet traffic to the
        internet gateway. Assumes an existing VPC.
        """
        rc = AWSInfResponseContext(spec=stack_spec)

        # check if we have subnets
        # todo: acually check with aws if subnets are up and running
        # todo: verify with aws if vpc and internet gateway are actually present.
        if not stack_spec.get("subnets"):
            vpc_id =domain_spec.get("vpc_id")
            internet_gateway_id = domain_spec.get("internet_gateway_id")
            try:
                stack_spec["subnets"] = []
                # create a subnet for this within the vpc
                data = self.vpc_conn.create_subnet(vpc_id=vpc_id, cidr_block=Dct.get(stack_spec, "cidr", "10.0.0.0/24"),
                                                   new_route_table=True, gateway_id=internet_gateway_id,
                                                   open_route_to_gateway=True)
                new_subnet = {"subnet_id":  data["subnet"].id, "route_table_id": data["route_table"].id,
                              "route_association_id": data["route_association_id"]}

                stack_spec["subnets"].append(new_subnet)

            except Exception, e:
                rc.errors.extend(e)

        # return updated spec
        rc.close(new_spec=stack_spec)
        return rc

    def delete_domain(self, domain_spec, delete_dependencies=False):
        """
        Delete the cluster
        """
        self.vpc_conn.delete_vpc(vpc_id=Dct.get(domain_spec, "vpc_id"), force=delete_dependencies)

    def provision_instances(self, domain_spec, stack_spec, instance_spec):
        """
        We need a subnet in the the instance_spec
        1. Create default security group with inbound traffic for:
            a. 22-22 (0.0.0.0/0)
            b. Custom auth_spec
        2. Provision a set of machines asynchronously as per spec.
        Returns a AWSInfRequestContext context
        """

        # required parameters
        domain_name = domain_spec.get("domain")
        vpc_id = domain_spec.get("vpc_id")
        subnet_id = stack_spec.get("subnets")[0]
        uname = Dct.get(instance_spec, "uname")
        image_id = Dct.get(instance_spec, "image_id")
        instance_type = Dct.get(instance_spec, "instance_type")
        key_pair_name = Dct.get(instance_spec, "key_pair_name")
        instance_count = Dct.get(instance_spec, "instance_count")
        associate_public_ip = Dct.get(instance_spec, "associate_public_ip", False)
        auth_spec = Dct.get(instance_spec, "auth_spec")
        tags = instance_spec.get('tags', {})

        self.log.info("Provision Instances: {0} for domain:{1}".format(instance_count, domain_name))
        rc = AwsInfProvisionResponseContext(spec=instance_spec)
        try:

            # create a security group
            self.log.info("Create Security group for domain:{0}".format(domain_name))
            sg = self.ec2_conn.get_or_create_group("sg_{0}".format(uname),
                                                   description="security group for {0}".format(uname),
                                                   auth_ssh=True, vpc_id=vpc_id, auth_spec=auth_spec)
            instance_spec["security_group_ids"] = [sg.id]

            # schedule instance creation.
            self.log.info("Request Instances:{0} for domain:{1}".format(instance_count, domain_name))
            rc.reservation = self.ec2_conn.request_instances(image_id=image_id,
                                                             instance_type=instance_type,
                                                             key_name=key_pair_name,
                                                             max_count=instance_count,
                                                             security_group_ids=instance_spec["security_group_ids"],
                                                             subnet_id=subnet_id,
                                                             associate_public_ip=associate_public_ip,
                                                             tags=tags)

            self.log.info("Waiting for instances to be ready")
            rc.close_on_instances_ready()

            self._fill_instance_details(rc.reservation.instances, instance_spec)
        except Exception, e:
            self.log.debug(msg="Failed provisioning instances", exc_info=e)
            rc.errors.extend(e)

        # return the context
        return rc


    def _fill_instance_details(self, instances, instance_spec):
        instance_spec['instances'] = []
        for instance in instances:
            instance.update()
            instance_spec['instances'].append({'instance_id': instance.id,
                                               'public_dns_name': instance.public_dns_name,
                                               'private_dns_name': instance.private_dns_name,
                                               'public_ip_address': instance.ip_address,
                                               'private_ip_address': instance.private_ip_address})

    def _get_ec2(self):
        return awsutils.EasyEC2(aws_access_key_id=self.aws_access_key,
                                aws_secret_access_key=self.aws_secret_key, aws_region_name="us-east-1")

    def _get_vpc(self):
        return awsutils.EasyVPC(aws_access_key_id=self.aws_access_key,
                                aws_secret_access_key=self.aws_secret_key, aws_region_name="us-east-1")