import boto
import logging
import yaml
import requests
from itertools import izip_longest
from pprint import pformat
from will import settings
from will.plugin import WillPlugin
from will.decorators import respond_to


class TooManyImagesException(Exception):
    pass


class Versions():

    def __init__(self, configuration_ref, configuration_secure_ref, versions):
        """
        configurations_ref: The gitref for configurations.
        configuration_secure_ref: The git ref for configuration_secure
        versions: A dict mapping version vars('.*_version') to gitrefs.
        """
        self.configurations = configuration_ref
        self.configuration_secure = configuration_secure_ref
        self.play_versions = versions


class ShowPlugin(WillPlugin):

    @respond_to("show (?P<env>\w*)(-(?P<dep>\w*))(-(?P<play>\w*))?")
    def show(self, message, env, dep, play):
        """show <e-d-p>: show the instances in a VPC cluster"""
        if play is None:
            self.show_plays(message, env, dep)
        else:
            self.show_edp(message, env, dep, play)

    @respond_to("show (?P<deployment>\w*) (?P<ami_id>ami-\w*)")
    def show_ami(self, message, deployment, ami_id):
        """show deployment <ami_id>: show tags for the ami"""
        ec2 = boto.connect_ec2(profile_name=deployment)
        amis = ec2.get_all_images(ami_id)
        if len(amis) == 0:
            self.say("No ami found with id {}".format(ami_id), message)
        else:
            for ami in amis:
                self.say("/code {}".format(pformat(ami.tags)), message)

    def show_plays(self, message, env, dep):
        logging.info("Getting all plays in {}-{}".format(env, dep))
        ec2 = boto.connect_ec2(profile_name=dep)

        instance_filter = {
            "tag:environment": env,
            "tag:deployment": dep,
        }
        instances = ec2.get_all_instances(filters=instance_filter)

        plays = set()
        for reservation in instances:
            for instance in reservation.instances:
                if "play" in instance.tags:
                    play_name = instance.tags["play"]
                    plays.add(play_name)

        output = ["Active Plays",
                  "------------"]
        output.extend(list(plays))
        self.say("/code {}".format("\n".join(output)), message)

    def instance_elbs(self, instance_id, profile_name=None, elbs=None):
        if elbs is None:
            elb = boto.connect_elb(profile_name=profile_name)
            elbs = elb.get_all_load_balancers()

        for lb in elbs:
            lb_instance_ids = [inst.id for inst in lb.instances]
            if instance_id in lb_instance_ids:
                yield lb

    def ami_for_edp(self, message, env, dep, play):
        ec2 = boto.connect_ec2(profile_name=dep)
        elb = boto.connect_elb(profile_name=dep)
        elbs = elb.get_all_load_balancers()

        edp_filter = {
            "tag:environment": env,
            "tag:deployment": dep,
            "tag:play": play,
        }
        reservations = ec2.get_all_instances(filters=edp_filter)
        amis = set()
        for reservation in reservations:
            for instance in reservation.instances:
                elbs = self.instance_elbs(instance.id, dep, elbs)
                if instance.state == 'running' and len(list(elbs)) > 0:
                    amis.add(instance.image_id)

        if len(amis) > 1:
            msg = "Multiple AMIs found for {}-{}-{}, there should " \
                "be only one. Please resolve any running deploys " \
                "there before running this command."
            msg = msg.format(env, dep, play)
            self.say(msg, message, color='red')
            return None

        return amis.pop()

    def show_edp(self, message, env, dep, play):
        self.say("Reticulating splines...", message)
        ec2 = boto.connect_ec2(profile_name=dep)
        edp_filter = {
            "tag:environment": env,
            "tag:deployment": dep,
            "tag:play": play,
        }
        instances = ec2.get_all_instances(filters=edp_filter)

        output_table = [
            ["Internal DNS", "Versions", "ELBs", "AMI"],
            ["------------", "--------", "----", "---"],
        ]
        instance_len, ref_len, elb_len, ami_len = map(len, output_table[0])

        for reservation in instances:
            for instance in reservation.instances:
                if instance.state != 'running':
                    continue
                msg = "Getting info for: {}"
                logging.info(msg.format(instance.private_dns_name))
                refs = []
                ami_id = instance.image_id
                for ami in ec2.get_all_images(ami_id):
                    for name, value in ami.tags.items():
                        if name.startswith('version:'):
                            refs.append(
                                "{}={}".format(name[8:], value.split()[1]))

                instance_name = lambda x: x.name
                elbs = map(instance_name,
                           self.instance_elbs(instance.id, dep))

                all_data = izip_longest(
                    [instance.private_dns_name],
                    refs, elbs, [ami_id],
                    fillvalue="",
                )
                for instance, ref, elb, ami in all_data:
                    output_table.append([instance, ref, elb, ami])
                    if instance:
                        instance_len = max(instance_len, len(instance))

                    if ref:
                        ref_len = max(ref_len, len(ref))

                    if elb:
                        elb_len = max(elb_len, len(elb))

                    if ami:
                        ami_len = max(ami_len, len(ami))

        output = []
        for line in output_table:
            output.append("{} {} {} {}".format(line[0].ljust(instance_len),
                                               line[1].ljust(ref_len),
                                               line[2].ljust(elb_len),
                                               line[3].ljust(ami_len),))

        logging.error(output_table)
        self.say("/code {}".format("\n".join(output)), message)

    def get_ami_versions(self, profile, ami_id):
        versions_dict = {}
        ec2 = boto.connect_ec2(profile_name=profile)
        ami = ec2.get_all_images(ami_id)[0]
        configuration_ref = None
        configuration_secure_ref = None
        # Build the versions_dict to have all versions defined in the ami tags
        for tag, value in ami.tags.items():
            if tag.startswith('version:'):
                key = tag[8:].strip()
                shorthash = value.split()[1]
                if key == 'configuration':
                    configuration_ref = shorthash
                elif key == 'configuration_secure':
                    configuration_secure_ref = shorthash
                else:
                    key = "{}_version".format(tag[8:])
                    # This is to deal with the fact that some
                    # versions are upper case and some are lower case.
                    versions_dict[key.lower()] = shorthash
                    versions_dict[key.upper()] = shorthash

        return Versions(configuration_ref,
                        configuration_secure_ref,
                        versions_dict
                        )

    @respond_to("(?P<noop>noop )?cut ami for "  # Initial words
                "(?P<env>\w*)-(?P<dep>\w*)-(?P<play>\w*)"  # Get the EDP
                "( from (?P<ami_id>ami-\w*))? "  # Optionally provide an ami
                "with(?P<versions>( \w*=\S*)*)")  # Override versions
    def build_ami(self, message, env, dep, play, versions,
                  ami_id=None, noop=False):
        """cut ami for: create a new ami from the given parameters"""
        versions_dict = {}
        configuration_ref = None
        configuration_secure_ref = None
        self.say("Let me get what I need to build the ami...", message)

        if ami_id:
            # Lookup the AMI and use its settings.
            self.say("Looking up ami {}".format(ami_id), message)
            ami_versions = self.get_ami_versions(dep, ami_id)
            configuration_ref = ami_versions.configuration
            configuration_secure_ref = ami_versions.configuration_secure
            versions_dict = ami_versions.play_versions

        if configuration_ref is None:
            configuration_ref = "master"
        if configuration_secure_ref is None:
            configuration_secure_ref = "master"

        # Override the ami and defaults with the setting from the user
        for version in versions.split():
            var, value = version.split('=')
            if var == 'configuration':
                configuration_ref = value
            elif var == 'configuration_secure':
                configuration_secure_ref = value
            else:
                versions_dict[var.lower()] = value
                versions_dict[var.upper()] = value

        final_versions = Versions(
            configuration_ref,
            configuration_secure_ref,
            versions_dict)

        self.notify_abbey(
            message, env, dep, play, final_versions, noop, ami_id)

    # A regex to build an AMI for one EDP from another EDP.
    @respond_to("(?P<verbose>verbose )?(?P<noop>noop )?cut ami for "  # Options
                "(?P<dest_env>\w*)-"  # Destination Environment
                "(?P<dest_dep>\w*)-"  # Destination Deployment
                "(?P<dest_play>\w*) "  # Destination Play(Cluster)
                "from "
                "(?P<source_env>\w*)-"  # Source Environment
                "(?P<source_dep>\w*)-"  # Source Deployment
                "(?P<source_play>\w*)"  # Destination Play(Cluster)
                "( with(?P<version_overrides>( \w*=\S*)*))?")  # Overrides
    def cut_from_edp(self, message, verbose, noop, dest_env, dest_dep,
                     dest_play, source_env, source_dep, source_play,
                     version_overrides):
        # Get the active source AMI.
        self.say("Let me get what I need to build the ami...", message)
        source_running_ami = self.ami_for_edp(
            message, source_env, source_dep, source_play)
        if source_running_ami is None:
            return

        source_versions = self.get_ami_versions(
            source_dep, source_running_ami, message)

        # Get the active destination AMI.  The one we're gonna
        # use as a base for our build.
        dest_running_ami = self.ami_for_edp(
            message, dest_env, dest_dep, dest_play)
        if dest_running_ami is None:
            return

        final_versions = self.update_from_versions_string(
            source_versions, version_overrides)

        self.notify_abbey(message, dest_env, dest_dep, dest_play,
                          final_versions, noop, dest_running_ami, verbose)

    def update_from_versions_string(self, defaults, versions_string, message):
        """Update with any version overrides defined in the versions_string."""
        if versions_string:
            for version in versions_string.split():
                var, value = version.split('=')
                msg = "Overriding '{}' for the new AMI."
                self.say(msg.format(var), message)
                if var == 'configuration':
                    defaults.configuration = value
                elif var == 'configuration_secure':
                    defaults.configuration_secure = value
                else:
                    defaults.play_versions[var.lower()] = value
                    defaults.play_versions[var.upper()] = value
        return defaults

    def notify_abbey(self, message, env, dep, play, versions,
                     noop=False, ami_id=None, verbose=False):

        if not hasattr(settings, 'JENKINS_URL'):
            msg = "The JENKINS_URL environment setting needs " \
                  "to be set so I can build AMIs."
            self.say(msg, message, color='red')
            return False
        else:
            abbey_url = settings.JENKINS_URL
            play_vars = yaml.safe_dump(
                versions.play_versions,
                default_flow_style=False,
            )
            params = {}
            params['play'] = play
            params['deployment'] = dep
            params['environment'] = env
            params['vars'] = play_vars
            params['configuration'] = versions.configuration
            params['configuration_secure'] = versions.configuration_secure
            if ami_id:
                params['base_ami'] = ami_id
                params['use_blessed'] = False
            else:
                params['use_blessed'] = True

            logging.info("Need ami for {}".format(pformat(params)))

            output = "Building ami for {}-{}-{}\n".format(env, dep, play)
            if verbose:
                if ami_id:
                    output += "With base ami: {}\n".format(ami_id)

                display_params = dict(params)
                display_params['vars'] = versions.play_versions
                output += yaml.safe_dump(
                    {"Params": display_params},
                    default_flow_style=False)

            self.say(output, message)

            if noop:
                r = requests.Request('POST', abbey_url, params=params)
                url = r.prepare().url
                self.say("Would have posted: {}".format(url), message)
            else:
                r = requests.post(abbey_url, params=params)

                logging.info("Sent request got {}".format(r))
                if r.status_code != 200:
                    self.say("Sent request got {}".format(r),
                             message, color='red')
