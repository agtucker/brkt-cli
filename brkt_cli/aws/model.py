# Copyright (c) 2006-2010 Mitch Garnaat http://garnaat.org/
# Copyright (c) 2010, Eucalyptus Systems, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish, dis-
# tribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the fol-
# lowing conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABIL-
# ITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT
# SHALL THE AUTHOR BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.


# These models are copied from boto2 and used by the unit test implementation
# of AWSService.


class EC2Object(object):

    def __init__(self, connection=None):
        self.connection = connection
        if self.connection and hasattr(self.connection, 'region'):
            self.region = connection.region
        else:
            self.region = None


class TaggedEC2Object(EC2Object):
    """
    Any EC2 resource that can be tagged should be represented
    by a Python object that subclasses this class.  This class
    has the mechanism in place to handle the tagSet element in
    the Describe* responses.  If tags are found, it will create
    a TagSet object and allow it to parse and collect the tags
    into a dict that is stored in the "tags" attribute of the
    object.
    """

    def __init__(self, connection=None):
        super(TaggedEC2Object, self).__init__(connection)
        self.tags = {}


class BlockDeviceMapping(dict):
    """
    Represents a collection of BlockDeviceTypes when creating ec2 instances.

    Example:
    dev_sda1 = BlockDeviceType()
    dev_sda1.size = 100   # change root volume to 100GB instead of default
    bdm = BlockDeviceMapping()
    bdm['/dev/sda1'] = dev_sda1
    reservation = image.run(..., block_device_map=bdm, ...)
    """

    def __init__(self, connection=None):
        """
        :type connection: :class:`boto.ec2.EC2Connection`
        :param connection: Optional connection.
        """
        dict.__init__(self)
        self.connection = connection
        self.current_name = None
        self.current_value = None


class BlockDeviceType(object):
    """
    Represents parameters for a block device.
    """

    def __init__(self,
                 connection=None,
                 ephemeral_name=None,
                 no_device=False,
                 volume_id=None,
                 snapshot_id=None,
                 status=None,
                 attach_time=None,
                 delete_on_termination=False,
                 size=None,
                 volume_type=None,
                 iops=None,
                 encrypted=None):
        self.connection = connection
        self.ephemeral_name = ephemeral_name
        self.no_device = no_device
        self.volume_id = volume_id
        self.snapshot_id = snapshot_id
        self.status = status
        self.attach_time = attach_time
        self.delete_on_termination = delete_on_termination
        self.size = size
        self.volume_type = volume_type
        self.iops = iops
        self.encrypted = encrypted


class ConsoleOutput(object):
    def __init__(self, parent=None):
        self.parent = parent
        self.instance_id = None
        self.timestamp = None
        self.output = None


class Image(TaggedEC2Object):
    """
    Represents an EC2 Image
    """

    def __init__(self, connection=None):
        super(Image, self).__init__(connection)
        self.id = None
        self.location = None
        self.state = None
        self.ownerId = None  # for backwards compatibility
        self.owner_id = None
        self.owner_alias = None
        self.is_public = False
        self.architecture = None
        self.platform = None
        self.type = None
        self.kernel_id = None
        self.ramdisk_id = None
        self.name = None
        self.description = None
        self.product_codes = None  # ProductCodes()
        self.billing_products = None  # BillingProducts()
        self.block_device_mapping = None
        self.root_device_type = None
        self.root_device_name = None
        self.virtualization_type = None
        self.hypervisor = None
        self.instance_lifecycle = None
        self.sriov_net_support = None

    def __repr__(self):
        return 'Image:%s' % self.id


class InstanceState(object):
    """
    The state of the instance.

    :ivar code: The low byte represents the state. The high byte is an
        opaque internal value and should be ignored.  Valid values:

        * 0 (pending)
        * 16 (running)
        * 32 (shutting-down)
        * 48 (terminated)
        * 64 (stopping)
        * 80 (stopped)

    :ivar name: The name of the state of the instance.  Valid values:

        * "pending"
        * "running"
        * "shutting-down"
        * "terminated"
        * "stopping"
        * "stopped"
    """
    def __init__(self, code=0, name=None):
        self.code = code
        self.name = name

    def __repr__(self):
        return '%s(%d)' % (self.name, self.code)


class InstancePlacement(object):
    """
    The location where the instance launched.

    :ivar zone: The Availability Zone of the instance.
    :ivar group_name: The name of the placement group the instance is
        in (for cluster compute instances).
    :ivar tenancy: The tenancy of the instance (if the instance is
        running within a VPC). An instance with a tenancy of dedicated
        runs on single-tenant hardware.
    """
    def __init__(self, zone=None, group_name=None, tenancy=None):
        self.zone = zone
        self.group_name = group_name
        self.tenancy = tenancy

    def __repr__(self):
        return self.zone

    def startElement(self, name, attrs, connection):
        pass

    def endElement(self, name, value, connection):
        if name == 'availabilityZone':
            self.zone = value
        elif name == 'groupName':
            self.group_name = value
        elif name == 'tenancy':
            self.tenancy = value
        else:
            setattr(self, name, value)


class Instance(TaggedEC2Object):
    """
    Represents an instance.

    :ivar id: The unique ID of the Instance.
    :ivar groups: A list of Group objects representing the security
                  groups associated with the instance.
    :ivar public_dns_name: The public dns name of the instance.
    :ivar private_dns_name: The private dns name of the instance.
    :ivar state: The string representation of the instance's current state.
    :ivar state_code: An integer representation of the instance's
        current state.
    :ivar previous_state: The string representation of the instance's
        previous state.
    :ivar previous_state_code: An integer representation of the
        instance's current state.
    :ivar key_name: The name of the SSH key associated with the instance.
    :ivar instance_type: The type of instance (e.g. m1.small).
    :ivar launch_time: The time the instance was launched.
    :ivar image_id: The ID of the AMI used to launch this instance.
    :ivar placement: The availability zone in which the instance is running.
    :ivar placement_group: The name of the placement group the instance
        is in (for cluster compute instances).
    :ivar placement_tenancy: The tenancy of the instance, if the instance
        is running within a VPC.  An instance with a tenancy of dedicated
        runs on a single-tenant hardware.
    :ivar kernel: The kernel associated with the instance.
    :ivar ramdisk: The ramdisk associated with the instance.
    :ivar architecture: The architecture of the image (i386|x86_64).
    :ivar hypervisor: The hypervisor used.
    :ivar virtualization_type: The type of virtualization used.
    :ivar product_codes: A list of product codes associated with this instance.
    :ivar ami_launch_index: This instances position within it's launch group.
    :ivar monitored: A boolean indicating whether monitoring is enabled or not.
    :ivar monitoring_state: A string value that contains the actual value
        of the monitoring element returned by EC2.
    :ivar spot_instance_request_id: The ID of the spot instance request
        if this is a spot instance.
    :ivar subnet_id: The VPC Subnet ID, if running in VPC.
    :ivar vpc_id: The VPC ID, if running in VPC.
    :ivar private_ip_address: The private IP address of the instance.
    :ivar ip_address: The public IP address of the instance.
    :ivar platform: Platform of the instance (e.g. Windows)
    :ivar root_device_name: The name of the root device.
    :ivar root_device_type: The root device type (ebs|instance-store).
    :ivar block_device_mapping: The Block Device Mapping for the instance.
    :ivar state_reason: The reason for the most recent state transition.
    :ivar groups: List of security Groups associated with the instance.
    :ivar interfaces: List of Elastic Network Interfaces associated with
        this instance.
    :ivar ebs_optimized: Whether instance is using optimized EBS volumes
        or not.
    :ivar instance_profile: A Python dict containing the instance
        profile id and arn associated with this instance.
    """

    def __init__(self, connection=None):
        super(Instance, self).__init__(connection)
        self.id = None
        self.dns_name = None
        self.public_dns_name = None
        self.private_dns_name = None
        self.key_name = None
        self.instance_type = None
        self.launch_time = None
        self.image_id = None
        self.kernel = None
        self.ramdisk = None
        self.product_codes = None  # ProductCodes()
        self.ami_launch_index = None
        self.monitored = False
        self.monitoring_state = None
        self.spot_instance_request_id = None
        self.subnet_id = None
        self.vpc_id = None
        self.private_ip_address = None
        self.ip_address = None
        self.requester_id = None
        self._in_monitoring_element = False
        self.persistent = False
        self.root_device_name = None
        self.root_device_type = None
        self.block_device_mapping = None
        self.state_reason = None
        self.group_name = None
        self.client_token = None
        self.eventsSet = None
        self.groups = []
        self.platform = None
        self.interfaces = []
        self.hypervisor = None
        self.virtualization_type = None
        self.architecture = None
        self.instance_profile = None
        self._previous_state = None
        self._state = InstanceState()
        self._placement = InstancePlacement()

    def __repr__(self):
        return 'Instance:%s' % self.id

    @property
    def state(self):
        return self._state.name

    @property
    def state_code(self):
        return self._state.code

    @property
    def placement(self):
        return self._placement.zone

    @property
    def placement_group(self):
        return self._placement.group_name

    @property
    def placement_tenancy(self):
        return self._placement.tenancy


class KeyPair(object):

    def __init__(self):
        self.name = None
        self.fingerprint = None
        self.material = None

    def __repr__(self):
        return 'KeyPair:%s' % self.name


class RegionInfo(object):
    """
    Represents an AWS Region
    """

    def __init__(self, connection=None, name=None, endpoint=None,
                 connection_cls=None):
        self.connection = connection
        self.name = name
        self.endpoint = endpoint
        self.connection_cls = connection_cls

    def __repr__(self):
        return 'RegionInfo:%s' % self.name


class SecurityGroup(TaggedEC2Object):

    def __init__(self, connection=None, owner_id=None,
                 name=None, description=None, id=None):
        super(SecurityGroup, self).__init__(connection)
        self.id = id
        self.owner_id = owner_id
        self.name = name
        self.description = description
        self.vpc_id = None
        self.rules = None  # IPPermissionsList()
        self.rules_egress = None  # IPPermissionsList()

    def __repr__(self):
        return 'SecurityGroup:%s' % self.name


class Snapshot(TaggedEC2Object):
    AttrName = 'createVolumePermission'

    def __init__(self, connection=None):
        super(Snapshot, self).__init__(connection)
        self.id = None
        self.volume_id = None
        self.status = None
        self.progress = None
        self.start_time = None
        self.owner_id = None
        self.owner_alias = None
        self.volume_size = None
        self.description = None
        self.encrypted = None

    def __repr__(self):
        return 'Snapshot:%s' % self.id


class Subnet(TaggedEC2Object):

    def __init__(self, connection=None):
        super(Subnet, self).__init__(connection)
        self.id = None
        self.vpc_id = None
        self.state = None
        self.cidr_block = None
        self.available_ip_address_count = 0
        self.availability_zone = None

    def __repr__(self):
        return 'Subnet:%s' % self.id


class Volume(TaggedEC2Object):
    """
    Represents an EBS volume.

    :ivar id: The unique ID of the volume.
    :ivar create_time: The timestamp of when the volume was created.
    :ivar status: The status of the volume.
    :ivar size: The size (in GB) of the volume.
    :ivar snapshot_id: The ID of the snapshot this volume was created
        from, if applicable.
    :ivar attach_data: An AttachmentSet object.
    :ivar zone: The availability zone this volume is in.
    :ivar type: The type of volume (standard or consistent-iops)
    :ivar iops: If this volume is of type consistent-iops, this is
        the number of IOPS provisioned (10-300).
    :ivar encrypted: True if this volume is encrypted.
    """

    def __init__(self, connection=None):
        super(Volume, self).__init__(connection)
        self.id = None
        self.create_time = None
        self.status = None
        self.size = None
        self.snapshot_id = None
        self.attach_data = None
        self.zone = None
        self.type = None
        self.iops = None
        self.encrypted = None

    def __repr__(self):
        return 'Volume:%s' % self.id


class VPC(TaggedEC2Object):

    def __init__(self, connection=None):
        """
        Represents a VPC.

        :ivar id: The unique ID of the VPC.
        :ivar dhcp_options_id: The ID of the set of DHCP options you've associated with the VPC
                                (or default if the default options are associated with the VPC).
        :ivar state: The current state of the VPC.
        :ivar cidr_block: The CIDR block for the VPC.
        :ivar is_default: Indicates whether the VPC is the default VPC.
        :ivar instance_tenancy: The allowed tenancy of instances launched into the VPC.
        :ivar classic_link_enabled: Indicates whether ClassicLink is enabled.
        """
        super(VPC, self).__init__(connection)
        self.id = None
        self.dhcp_options_id = None
        self.state = None
        self.cidr_block = None
        self.is_default = None
        self.instance_tenancy = None
        self.classic_link_enabled = None

    def __repr__(self):
        return 'VPC:%s' % self.id
