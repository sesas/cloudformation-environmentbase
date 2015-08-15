import os
import os.path
import copy
import sys
import time
import re
import botocore.exceptions
import troposphere.cloudformation as cf
from troposphere import Ref, Parameter, GetAtt
from template import Template
import cli
import resources as res
from fnmatch import fnmatch
import utility

# Allow comments in json if you can but at least parse regular json if not
try:
    import commentjson as json
    from commentjson import JSONLibraryException as ValueError
except ImportError:
    import json

# If you run into compatibility issues, use the regular json library instead:
import json as pure_json

TIMEOUT = 60
TEMPLATES_PATH = 'templates'


class ValidationError(Exception):
    pass


class EnvironmentBase(object):
    """
    EnvironmentBase encapsulates functionality required to build and deploy a network and common resources for object storage within a specified region
    """

    config_filename = None
    config = {}
    globals = {}
    template_args = {}
    template = None
    manual_parameter_bindings = {}
    deploy_parameter_bindings = []
    ignore_outputs = ['templateValidationHash', 'dateGenerated']
    stack_outputs = {}
    config_handlers = []

    boto_session = None

    stack_event_handlers = []

    def __init__(self,
                 view=None,
                 create_missing_files=True,
                 config_filename=res.DEFAULT_CONFIG_FILENAME,
                 config=None):
        """
        Init method for environment base creates all common objects for a given environment within the CloudFormation
        template including a network, s3 bucket and requisite policies to allow ELB Access log aggregation and
        CloudTrail log storage.
        :param view: View object to use.
        :param create_missing_files: Specifies policy to use when local files are missing.  When disabled missing files will cause an IOException
        :param config_filename: The name of the config file to load by default.  Note: User can still override this value from the CLI with '--config-file'.
        :param config: Override loading config values from file by providing config setting directly to the constructor
        """

        # Load the user interface
        if view is None:
            view = cli.CLI()

        # Config filename check has to happen now because the rest of the settings rely on having a loaded config file
        if hasattr(view, 'config_filename') and view.config_filename is not None:
            self.config_filename = view.config_filename
        else:
            self.config_filename = config_filename

        # Config location override
        self.create_missing_files = create_missing_files
        self.handle_local_config(config)

        # Process any global flags here before letting the view execute any requested user actions
        view.update_config(self.config)

        # Shortcut references to config sections
        self.globals = self.config.get('global', {})
        self.template_args = self.config.get('template', {})

        # Finally allow the view to execute the user's requested action
        view.process_request(self)

    def _ensure_template_dir_exists(self):
        parent_dir = TEMPLATES_PATH
        if not os.path.exists(parent_dir):
            os.makedirs(parent_dir)
        path = os.path.join(TEMPLATES_PATH, self.config['global']['output'])
        return path

    def write_template_to_file(self):
        """
        Serializes self.template to string and writes it to the file named in config['global']['output']
        """
        local_path = self._ensure_template_dir_exists()

        with open(local_path, 'w') as output_file:
            # Here to_json() loads child templates into S3
            raw_json = self.template.to_template_json()

            reloaded_template = pure_json.loads(raw_json)
            pure_json.dump(reloaded_template, output_file, indent=4, separators=(',', ':'))

    def create_action(self):
        """
        Default create_action invoked by the CLI
        Initializes a new template instance, and write it to file.
        """
        self.initialize_template()

        # Do custom troposphere resource creation in your overridden copy of this method

        self.write_template_to_file()

    def _update_config_from_env(self, section_label, config_key):
        """
        Update config value with values from the environment variables. For each subsection below the 'section_label'
        containing the 'config_key' the environment is scanned to find an environment variable matching the name
        <subsection_label>_<config_key> (in all caps). If thais variable exists the config value is replaced.

        For example: self._update_config_from_env('db', 'password') for the config file:
        {
            ...
            'db': {
                'label1': {
                    ...
                    'password': 'changeme'
                },
                'label2': {
                    ...
                    'password': 'changeme]'
                }
            }
        }

        Would replace those two database passwords if the following is run from the shell:
        > export LABEL1_PASSWORD=myvoiceismypassword12345
        > export LABEL2_PASSWORD=myvoiceismyotherpassword12345
        """
        config_section = self.config.get(section_label)
        if config_section is None:
            raise ValueError('No config section %s found' % config_key)

        # TODO: handle direct change in case where we don't need subsections

        update_set = {}
        for subsection_label, subsection in config_section.iteritems():

            # Look for env var: <subsection_label> '_' <config key> (e.g. proddb, password --> PRODDB_PASSWORD)
            env_name = ("%s_%s" % (subsection_label, config_key)).upper()

            # Save the old value and the new value
            env_value = os.environ.get(env_name)
            default_value = subsection.get(config_key)

            # If an env var was found override old value in a separate map
            update_set[subsection_label] = env_value if env_value else default_value

            if self.config['global']['print_debug']:
                print "%s.%s.%s = '%s'" % (section_label, subsection_label, config_key, default_value)
                if env_value:
                    print "* Value updated to", "'{}'".format(env_value), "({})".format(env_name)
                else:
                    print "* Value NOT updated since '%s' not found" % env_name
                print

        # process the map of updates and make the actual changes
        for subsection_label, updated_value in update_set.iteritems():
            config_section[subsection_label][config_key] = updated_value

    def load_db_passwords_from_env(self):
        self._update_config_from_env('db', 'password')

    def setup_stack_monitor(self):
        # Topic and queue names are randomly generated so there's no chance of picking up messages from a previous runs
        name = self.config['global']['environment_name'] + '_' + time.strftime("%Y%m%d-%H%M%S") + '_' + utility.random_string(5)

        # Creating a topic is idempotent, so if it already exists then we will just get the topic returned.
        sns = utility.get_boto_resource(self.config, 'sns')
        topic_arn = sns.create_topic(Name=name).arn

        # Creating a queue is idempotent, so if it already exists then we will just get the queue returned.
        sqs = utility.get_boto_resource(self.config, 'sqs')
        queue = sqs.create_queue(QueueName=name)

        queue_arn = queue.attributes['QueueArn']

        # Ensure that we are subscribed to the SNS topic
        subscribed = False
        topic = sns.Topic(topic_arn)
        for subscription in topic.subscriptions.all():
            if subscription.attributes['Endpoint'] == queue_arn:
                subscribed = True
                break

        if not subscribed:
            topic.subscribe(Protocol='sqs', Endpoint=queue_arn)

        # Set up a policy to allow SNS access to the queue
        if 'Policy' in queue.attributes:
            policy = json.loads(queue.attributes['Policy'])
        else:
            policy = {'Version': '2008-10-17'}

        if 'Statement' not in policy:
            statement = {
                "Sid": "sqs-access",
                "Effect": "Allow",
                "Principal": {"AWS": "*"},
                "Action": "SQS:SendMessage",
                "Resource": "<SQS QUEUE ARN>",
                "Condition": {"StringLike": {"aws:SourceArn": "<SNS TOPIC ARN>"}}
            }
            statement['Resource'] = queue_arn
            statement['Condition']['StringLike']['aws:SourceArn'] = topic_arn
            policy['Statement'] = [statement]

            queue.set_attributes(Attributes={
                'Policy': json.dumps(policy)
            })

        return topic, queue

    def start_stack_monitor(self, queue, stack_name):
        TERMINAL_STATES = [
            'CREATE_COMPLETE',
            'UPDATE_COMPLETE',
            'UPDATE_ROLLBACK_COMPLETE',
            'CREATE_FAILED',
            'UPDATE_FAILED',
            'UPDATE_ROLLBACK_FAILED',
        ]
        # Process messages by printing out body and optional author name
        poll_timeout = 3600  # an hour
        poll_interval = 5
        start_time = time.time()
        time.clock()
        elapsed = 0
        is_stack_running = True

        while elapsed < poll_timeout and is_stack_running and len(self.stack_event_handlers) > 0:

            elapsed = time.time() - start_time

            msgs = queue.receive_messages(WaitTimeSeconds=poll_interval, MaxNumberOfMessages=10)
            # print 'grabbed batch of %s' % len(msgs)

            for raw_msg in msgs:
                parsed_msg = json.loads(raw_msg.body)
                msg_body = parsed_msg['Message']

                # parse k='val' into a dict
                parsed_msg = {k: v.strip("'") for k, v in re.findall(r"(\S+)=('.*?'|\S+)", msg_body)}

                # remember the most interesting outputs
                data = {
                    "status": parsed_msg.get('ResourceStatus'),
                    "type": parsed_msg.get('ResourceType'),
                    "name": parsed_msg.get('LogicalResourceId'),
                    "reason": parsed_msg.get('ResourceStatusReason'),
                    "props": parsed_msg.get('ResourceProperties')
                }

                # attempt to parse the properties
                try:
                    data['props'] = json.loads(data['props'])
                except ValueError:
                    pass

                if self.config['global']['print_debug']:
                    print "New Stack Event --------------\n", \
                        data['status'], data['type'], data['name'], '\n', \
                        data['reason'], '\n', \
                        json.dumps(data['props'], indent=4)
                else:
                    pass

                # clear the message
                raw_msg.delete()

                # process handlers
                handlers_to_remove = []
                for handler in self.stack_event_handlers:
                    if handler.handle_stack_event(data):
                        handlers_to_remove.append(handler)

                # once a handlers job is done no need to keep checking for more events
                for handler in handlers_to_remove:
                    self.stack_event_handlers.remove(handler)

                # Finally test for the termination condition
                if data['type'] == "AWS::CloudFormation::Stack" \
                        and data['name'] == stack_name \
                        and data['status'] in TERMINAL_STATES:
                    is_stack_running = False
                    # print 'termination condition found!'

    def cleanup_stack_monitor(self, topic, queue):
        if topic:
            topic.delete()
        if queue:
            queue.delete()

    def add_stack_event_handler(self, handler):
        self.stack_event_handlers.append(handler)

    def _load_root_template(self):
        # Validate existence of and read in the template file
        cfn_template_filename = os.path.join(TEMPLATES_PATH, self.config['global']['output'])
        if os.path.isfile(cfn_template_filename):
            with open(cfn_template_filename, 'r') as cfn_template_file:
                cfn_template = cfn_template_file.read()
            white_space = re.compile(r'\s+')
            cfn_template = re.sub(white_space, ' ', cfn_template)
        else:
            raise ValueError('Template at: %s not found\n' % cfn_template_filename)

        return cfn_template

    def _ensure_stack_is_deployed(self, stack_name='UnnamedStack', sns_topic=None, stack_params=[]):
        notification_arns = []

        if sns_topic:
            notification_arns.append(sns_topic.arn)

        cfn_template = self._load_root_template()
        cfn_conn = utility.get_boto_client(self.config, 'cloudformation')
        try:
            print "Updating stack '%s' ..." % stack_name
            cfn_conn.update_stack(
                StackName=stack_name,
                TemplateBody=cfn_template,
                Parameters=stack_params,
                NotificationARNs=notification_arns,
                Capabilities=['CAPABILITY_IAM'])
            print "[Update success]"

        # Else stack doesn't currently exist, create a new stack
        except botocore.exceptions.ClientError as e:
            print "[Update failed], %s\n" % e.message
            print "Trying create stack ..."
            # Load template to string
            try:
                cfn_conn.create_stack(
                    StackName=stack_name,
                    TemplateBody=cfn_template,
                    Parameters=stack_params,
                    NotificationARNs=notification_arns,
                    Capabilities=['CAPABILITY_IAM'],
                    DisableRollback=True,
                    TimeoutInMinutes=TIMEOUT)
                print "Created new CF stack %s\n" % stack_name
            except botocore.exceptions.ClientError as e:
                print "[Create failed], %s\nExiting" % e.message

    def deploy_action(self):
        """
        Default deploy_action invoked by the CLI. Attempt to update the stack. If the stack does not yet exist, it will
        issue a create-stack command.
        """

        # gather runtime parameters to be passed to create/update stack
        stack_params = []
        if self.deploy_parameter_bindings:
            stack_params.extend(self.deploy_parameter_bindings)

        # initialize stack event monitor
        topic = None
        queue = None
        if len(self.stack_event_handlers) > 0:
            (topic, queue) = self.setup_stack_monitor()

        # Get url to cost estimate calculator
        # estimate_cost_url = cfn_conn.estimate_template_cost(
        #     TemplateBody=cfn_template,
        #     Parameters=stack_params).get('Url')
        # print estimate_cost_url

        # First try to do an update-stack... if it doesn't exist, then try create-stack
        stack_name = self.config['global']['environment_name']
        self._ensure_stack_is_deployed(
            stack_name,
            sns_topic=topic,
            stack_params=stack_params)

        try:
            self.start_stack_monitor(queue, stack_name)
        except KeyboardInterrupt:
            print 'KeyboardInterrupt: calling cleanup'
            self.cleanup_stack_monitor(topic, queue)
            raise

        self.cleanup_stack_monitor(topic, queue)

    def delete_action(self):
        """
        Default delete_action invoked by CLI
        """
        cfn_conn = utility.get_boto_client(self.config, 'cloudformation')
        stack_name = self.config['global']['environment_name']

        print "Deleting stack '%s' ..." % stack_name,
        cfn_conn.delete_stack(StackName=stack_name)
        print "Done"

    def _validate_config_helper(self, schema, config, path):
        # Check each requirement
        for (req_key, req_value) in schema.iteritems():

            # Check for key match, usually only one match but parametrized keys can have multiple matches
            # Uses 'filename' match, similar to regex but only supports '?', '*', [XYZ], [!XYZ]
            filter_fun = lambda candidate_key: fnmatch(candidate_key, req_key)

            # Find all config keys matching the requirement
            matches = filter(filter_fun, config.keys())
            if not matches:
                message = "Config file missing section " + str(path) + ('.' if path is not '' else '') + req_key
                raise ValidationError(message)

            # Validate each matching config entry
            for matching_key in matches:
                new_path = path + ('.' if path is not '' else '') + matching_key

                # ------------ value check -----------
                if isinstance(req_value, basestring):
                    req_type = res.get_type(req_value)

                    if not isinstance(config[matching_key], req_type):
                        message = "Type mismatch in config, %s should be of type %s, not %s" % \
                                  (new_path, req_value, type(config[matching_key]).__name__)
                        raise ValidationError(message)
                    # else:
                    #     print "%s validated: %s == %s" % (new_path, req_value, type(config[matching_key]).__name__)

                # if the schema is nested another level .. we must go deeper
                elif isinstance(req_value, dict):
                    matching_value = config[matching_key]
                    if not isinstance(matching_value, dict):
                        message = "Type mismatch in config, %s should be a dict, not %s" % \
                                  (new_path, type(matching_value).__name__)
                        raise ValidationError(message)

                    self._validate_config_helper(req_value, matching_value, new_path)

    def _validate_config(self, config, factory_schema=res.CONFIG_REQUIREMENTS):
        """
        Compares provided dict against TEMPLATE_REQUIREMENTS. Checks that required all sections and values are present
        and that the required types match. Throws ValidationError if not valid.
        :param config: dict to be validated
        """
        config_reqs_copy = copy.deepcopy(factory_schema)

        # Merge in any requirements provided by config handlers
        for handler in self.config_handlers:
            config_reqs_copy.update(handler.get_config_schema())

        self._validate_config_helper(config_reqs_copy, config, '')

    def add_config_handler(self, handler):
        """
        Register classes that will augment the configuration defaults and/or validation logic here
        """

        if not hasattr(handler, 'get_factory_defaults') or not callable(getattr(handler, 'get_factory_defaults')):
            raise ValidationError('Class %s cannot be a config handler, missing get_factory_defaults()' % type(handler).__name__ )

        if not hasattr(handler, 'get_config_schema') or not callable(getattr(handler, 'get_config_schema')):
            raise ValidationError('Class %s cannot be a config handler, missing get_config_schema()' % type(handler).__name__ )

        self.config_handlers.append(handler)

    def handle_local_config(self, config=None):
        """
        Use local file if present, otherwise use factory values and write that to disk
        unless self.create_missing_files == false, in which case throw IOError
        """

        if not config:

            # If override config file exists, use it
            if os.path.isfile(self.config_filename):
                with open(self.config_filename, 'r') as f:
                    content = f.read()
                    try:
                        config = json.loads(content)
                    except ValueError:
                        print '%s could not be parsed' % self.config_filename
                        raise

            # If we are instructed to create fresh override file, do it
            # unless the filename is something other than DEFAULT_CONFIG_FILENAME
            elif self.create_missing_files and self.config_filename == res.DEFAULT_CONFIG_FILENAME:
                default_config_copy = copy.deepcopy(res.FACTORY_DEFAULT_CONFIG)

                # Merge in any defaults provided by registered config handlers
                for handler in self.config_handlers:
                    default_config_copy.update(handler.get_factory_defaults())

                # Don't want changes to config modifying the FACTORY_DEFAULT
                config = copy.deepcopy(default_config_copy)

                with open(self.config_filename, 'w') as f:
                    f.write(json.dumps(default_config_copy, indent=4, sort_keys=True, separators=(',', ': ')))

            # Otherwise complain
            else:
                raise IOError(self.config_filename + ' could not be found')

        # Validate and save results
        self._validate_config(config)
        self.config = config

    def initialize_template(self):
        """
        Create new Template instance, set description and common parameters and load AMI cache.
        """
        self.template = Template(self.globals.get('output', 'default_template'))

        self.template.description = self.template_args.get('description', 'No Description Specified')
        self.init_root_template(self.template_args)
        EnvironmentBase.load_ami_cache(self.template, self.create_missing_files)

    @staticmethod
    def load_ami_cache(template, create_missing_files=True):
        """
        Method gets the ami cache from the file locally and adds a mapping for ami ids per region into the template
        This depends on populating ami_cache.json with the AMI ids that are output by the packer scripts per region
        @param template The template to attach the AMI mapping to
        @param create_missing_file File loading policy, if true
        """
        file_path = None

        # Users can provide override ami_cache in their project root
        local_amicache = os.path.join(os.getcwd(), res.DEFAULT_AMI_CACHE_FILENAME)
        if os.path.isfile(local_amicache):
            file_path = local_amicache

        # Or sibling to the executing class
        elif os.path.isfile(res.DEFAULT_AMI_CACHE_FILENAME):
            file_path = res.DEFAULT_AMI_CACHE_FILENAME

        if file_path:
            with open(file_path, 'r') as json_file:
                json_data = json.load(json_file)
        elif create_missing_files:
            json_data = res.FACTORY_DEFAULT_AMI_CACHE
            with open(res.DEFAULT_AMI_CACHE_FILENAME, 'w') as f:
                f.write(json.dumps(res.FACTORY_DEFAULT_AMI_CACHE, indent=4, separators=(',', ': ')))
        else:
            raise IOError(res.DEFAULT_AMI_CACHE_FILENAME + ' could not be found')

        template.add_ami_mapping(json_data)

    def init_root_template(self, template_config):
        """
        Adds common parameters for instance creation to the CloudFormation template
        @param template_config [dict] collection of template-level configuration values to drive the setup of this method
        """
        self.template.add_parameter_idempotent(Parameter('ec2Key',
                Type='String',
                Default=template_config.get('ec2_key_default', 'default-key'),
                Description='Name of an existing EC2 KeyPair to enable SSH access to the instances',
                AllowedPattern=res.get_str('ec2_key'),
                MinLength=1,
                MaxLength=255,
                ConstraintDescription=res.get_str('ec2_key_message')))

        self.remote_access_cidr = self.template.add_parameter(Parameter('remoteAccessLocation',
                Description='CIDR block identifying the network address space that will be allowed to ingress into public access points within this solution',
                Type='String',
                Default='0.0.0.0/0',
                MinLength=9,
                MaxLength=18,
                AllowedPattern=res.get_str('cidr_regex'),
                ConstraintDescription=res.get_str('cidr_regex_message')))

        self.template.add_utility_bucket(
            name=template_config.get('utility_bucket'),
            param_binding_map=self.manual_parameter_bindings)

    def to_json(self):
        """
        Centralized method for managing outputting this template with a timestamp identifying when it was generated and for creating a SHA256 hash representing the template for validation purposes
        """
        return self.template.to_template_json()

    def add_common_params_to_child_template(self, template):
        az_count = self.config['network']['az_count']
        subnet_types = self.config['network']['subnet_types']
        template.add_common_parameters(subnet_types, az_count)

        template.add_parameter_idempotent(Parameter(
            'ec2Key',
            Type='String',
            Default=self.config.get('template').get('ec2_key_default', 'default-key'),
            Description='Name of an existing EC2 KeyPair to enable SSH access to the instances',
            AllowedPattern=res.get_str('ec2_key'),
            MinLength=1,
            MaxLength=255,
            ConstraintDescription=res.get_str('ec2_key_message')))

    # Called after add_child_template() has attached common parameters and some instance attributes:
    # - RegionMap: Region to AMI map, allows template to be deployed in different regions without updating AMI ids
    # - ec2Key: keyname to use for ssh authentication
    # - vpcCidr: IP block claimed by whole VPC
    # - vpcId: resource id of VPC
    # - commonSecurityGroup: sg identifier for common allowed ports (22 in from VPC)
    # - utilityBucket: S3 bucket name used to send logs to
    # - availabilityZone[1-3]: Indexed names of AZs VPC is deployed to
    # - [public|private]Subnet[0-9]: indexed and classified subnet identifiers
    #
    # and some instance attributes referencing the attached parameters:
    # - self.vpc_cidr
    # - self.vpc_id
    # - self.common_security_group
    # - self.utility_bucket
    # - self.subnets: keyed by type and index (e.g. self.subnets['public'][1])
    # - self.azs: List of parameter references
    def add_child_template(self,
                           template,
                           template_bucket=None,
                           s3_template_prefix=None,
                           template_upload_acl=None,
                           depends_on=[]):
        """
        Method adds a child template to this object's template and binds the child template parameters to properties, resources and other stack outputs
        @param template [Troposphere.Template] Troposphere Template object to add as a child to this object's template
        @param template_bucket [str] name of the bucket to upload keys to - will default to value in template_args if not present
        @param s3_template_prefix [str] s3 key name prefix to prepend to s3 key path - will default to value in template_args if not present
        @param template_upload_acl [str] name of the s3 canned acl to apply to templates uploaded to S3 - will default to value in template_args if not present
        """
        name = template.name

        self.add_common_params_to_child_template(template)
        self.load_ami_cache(template)

        template.build_hook()

        stack_url = self.upload_template(
            template,
            template_bucket=template_bucket,
            s3_template_prefix=s3_template_prefix,
            template_upload_acl=template_upload_acl)

        if name not in self.stack_outputs:
            self.stack_outputs[name] = []

        stack_params = {}

        for parameter in template.parameters.keys():
            # Manual parameter bindings single-namespace
            if parameter in self.manual_parameter_bindings:
                stack_params[parameter] = self.manual_parameter_bindings[parameter]

            # Naming scheme for identifying the AZ of a subnet (not sure if this is even used anywhere)
            elif parameter.startswith('availabilityZone'):
                stack_params[parameter] = GetAtt('privateSubnet' + parameter.replace('availabilityZone', ''), 'AvailabilityZone')

            # Match any child stack parameters that have the same name as this stacks **parameters**
            elif parameter in self.template.parameters.keys():
                stack_params[parameter] = Ref(self.template.parameters.get(parameter))

            # Match any child stack parameters that have the same name as this stacks **resources**
            elif parameter in self.template.resources.keys():
                stack_params[parameter] = Ref(self.template.resources.get(parameter))

            # Match any child stack parameters that have the same name as this stacks **outputs**
            # TODO: Does this even work? Child runs after parent completes?
            elif parameter in self.stack_outputs:
                stack_params[parameter] = GetAtt(self.stack_outputs[parameter], 'Outputs.' + parameter)

            # Finally if nothing else matches copy the child templates parameter to this template's parameter list
            # so the value will pass through this stack down to the child.
            else:
                stack_params[parameter] = Ref(self.template.add_parameter(template.parameters[parameter]))
        stack_name = name + 'Stack'

        stack_obj = cf.Stack(
            stack_name,
            TemplateURL=stack_url,
            Parameters=stack_params,
            TimeoutInMinutes=self.template_args.get('timeout_in_minutes', '60'),
            DependsOn=depends_on)

        return self.template.add_resource(stack_obj)

    def upload_template(self,
                        template,
                        template_bucket=None,
                        s3_template_prefix=None,
                        template_upload_acl=None):
        """
        Upload helper to upload this template to S3 for consumption by other templates or end users.
        @param template [Template] object to be uploaded to s3.
        @param template_bucket [string] name of the AWS S3 bucket to upload this template to.
        @param s3_template_prefix [string] key name prefix to prepend to the key name for the upload of this template.
        @param template_upload_acl [string] S3 canned ACL string value to use when setting permissions on uploaded key.
        """
        key_serial = str(int(time.time()))

        if s3_template_prefix is None:
            s3_template_prefix = self.template_args.get("s3_template_prefix")

        if template_bucket is None:
            template_bucket = self.template_args.get('template_bucket')

        if template_upload_acl is None:
            template_upload_acl = self.template_args.get('template_upload_acl')

        template_name = "%s.%s.template" % (template.name, key_serial)
        s3_path = "%s/%s" % (s3_template_prefix, template_name)
        local_path = self._ensure_template_dir_exists()

        if self.config['global']['print_debug']:
            with open(local_path, 'w') as f:
                f.write(self.to_json())

        s3 = utility.get_boto_resource(self.config, 's3')

        s3.Bucket(template_bucket).put_object(
            Key=s3_path,
            Body=template.to_json(),
            ACL=template_upload_acl
        )

        stack_url = 'https://%s.s3.amazonaws.com/%s' % (template_bucket, s3_path)
        return stack_url

if __name__ == '__main__':
    EnvironmentBase()
