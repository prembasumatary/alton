from will.plugin import WillPlugin
from will.decorators import respond_to, require_settings

from alton.user import User, requires_permission

import logging
log = logging.getLogger(__name__)


def warn_direct_message(func):
    def check_direct_message(plugin, message, *args, **kwargs):
        if message['type'] != 'chat':
            plugin.reply(message, "This command is sensitive, so I will reply to you in a private message")
        return func(plugin, message, *args, **kwargs)

    return check_direct_message


class UserPlugin(WillPlugin):
    """
    Manage users, authentication and authorization.

    The single layer of password security for hipchat is not sufficient by our estimation for some of the tasks we want
    to automate. In order to ensure that users are actually who they claim they are, we implemented an additional layer
    of security in the form of a time sensitive token that is generated by an external device. Google Authenticator is
    the recommended smartphone app which is capable of generating these tokens.

    Once a user is authenticated, they have a finite amount of time to execute privileged actions. Once that time limit
    is exceeded, they will have to re-authenticate. Similar to the "sudo" command in *nix. Thus, if a user's laptop is
    stolen, the attacker would have a very small window in which they could execute privileged commands even if the
    computer is still logged into hipchat.

    The settings used to configure this plugin are (can be placed in config.py):

    The following settings are required and should be passed in through environment variables (for security reasons):

    TWOFACTOR_SECRET: This is secret key that is used to encrypt sensitive data in redis. If redis is compromised, user
        tokens would not also be compromised unless this secret was also known.

    TWOFACTOR_S3_BUCKET: The S3 bucket in which QR code images are stored. This bucket should only be readable by the
        AWS account which uploads the images. Short lived access will be allowed to the images when they are uploaded to
        allow the user enough time to retrieve the image.

    ADMIN_USERS: This is expected to be a comma separated list of users who will be automatically granted administrative
        privileges. They will be able to manage the authentication of other users etc. At least one user must have
        administrative privileges in order to grant the permission to grant permissions to other users.

    The following settings are optional, but can be used to configure behavior:

    TWOFACTOR_SESSION_DURATION (default = 1800): The number of seconds an authenticated session should last. After this
        amount of time has elapsed since a user last verified they are automatically logged out.

    TWOFACTOR_QR_CODE_VALIDITY_DURATION (default = 600): The number of seconds the URL for a generated QR code should be
        "live". Once the code is generated, the user has this many seconds to click the link. If they fail to click the
        link during that time the image will no longer be accessible and an admin will have to unlock their account.

    TWOFACTOR_ISSUER (default = Alton): This is the name of the bot (typically). It will appear above the user's name in
        the app that generates the rotating keys to identify the code. Many of the apps will generate keys for several
        providers, so this string should uniquely identify your bot.

    TWOFACTOR_S3_PROFILE (default = None): The boto profile to use to connect to S3. The machine that is running the bot can
        have a boto configuration file present that contains the various credentials needed to connect to AWS to perform
        various actions. The credentials referenced by this profile must be able to be used to upload new keys to the S3
        bucket specified by TWOFACTOR_S3_BUCKET and generate URLs for those objects. If no profile is provided, the
        credentials are expected to be provided via other means (default credentials, environment vars, IAM role etc).
    """

    @require_settings("TWOFACTOR_SECRET", "TWOFACTOR_ISSUER", "TWOFACTOR_S3_BUCKET", "TWOFACTOR_S3_PROFILE")
    @respond_to("^twofactor verify (?P<token>\w+)")
    @warn_direct_message
    def verify_user_twofactor(self, message, token):
        """twofactor verify [token]: start a new authenticated session by providing a verification token generated by the external device"""
        user = User.get_from_message(self, message)
        if not user:
            return

        if user.verify_token(token):
            self.direct_reply(message, "You are authenticated, your session expires {}".format(self.to_natural_day_and_time(user.session_expiration_time)))
        else:
            self.direct_reply(message, "Authentication failed, please try again")

        user.save()

    def direct_reply(self, message, content, **kwargs):
        self.send_direct_message(message.sender["hipchat_id"], content, **kwargs)

    @respond_to("^twofactor me")
    @warn_direct_message
    def create_user_twofactor(self, message):
        """twofactor me: generate a new QR code that can be used to configure the external device to generate valid verification tokens"""
        user = User.get(self, message.sender)
        if not user:
            user = User.create(self, message.sender)
            self.direct_reply(message, user.generate_and_upload_qr_code_image(), html=True)
            user.save()
            self.direct_reply(message, "Say 'twofactor verify [token]' to start an authenticated session")
        else:
            self.direct_reply(message, "twofactor is already configured")

    @respond_to("^twofactor status")
    @warn_direct_message
    def twofactor_status(self, message):
        """twofactor status: tells you if you are currently authenticated and when your session ends"""
        user = User.get_from_message(self, message)
        if not user:
            return

        if user.is_authenticated:
            self.direct_reply(message, "You are authenticated, your session expires {}".format(self.to_natural_day_and_time(user.session_expiration_time)))
        else:
            self.direct_reply(message, "You are not authenticated")

    @respond_to("^twofactor logout")
    @warn_direct_message
    def twofactor_logout(self, message):
        """twofactor logout: terminate an authenticated session"""
        user = User.get_from_message(self, message)
        if not user:
            return

        user.logout()
        user.save()
        self.direct_reply(message, "Your authenticated session has been terminated")

    @respond_to("^twofactor remove (?P<nick>\w+)")
    @requires_permission('administer_twofactor')
    def remove_user_twofactor(self, message, nick):
        """twofactor remove [nick]: remove [nick]'s twofactor authentication (requires the 'administer_twofactor' permission)"""
        self.reply(message, "working on that...")
        user_to_remove = User.get_from_nick(self, nick)
        if user_to_remove:
            user_to_remove.delete()
            self.direct_reply(message, "Successfully removed twofactor authentication for user '{}'".format(nick))
        else:
            self.direct_reply(message, "I could not find user '{}', no action was taken".format(nick))


    @respond_to("^twofactor help")
    def show_twofactor_help(self, message):
        """twofactor help: Show twofactor help"""
        self.reply(message, " <b>Two Factor Help:</b><br/>\
        &nbsp; <b>twofactor me:</b>  set up two factor authentication.<br/>\
        &nbsp; <b>twofactor verify [token]</b>:  Start a two factor verified session<br/>\
        &nbsp; <b>twofactor status</b>: tells you if you are currently authenticated and when your session ends<br/>\
        &nbsp; <b>twofactor logout</b>: terminate an authenticated session<br/>\
        <b>Admin Functions</b></br>\
        &nbsp; <b>twofactor remove [nick]</b>: remove [nick]'s twofactor authentication (requires the 'administer_twofactor' permission)", html=True)

    @respond_to("^permissions help")
    def show_permissions_help(self, message):
        """permissions help: Show permissions help"""
        self.reply(message, " <b>Permissions Help:</b><br/>\
        &nbsp; <b>what can [nick] do</b>: get permissions for a user<br/>\
        &nbsp; <b>who can [permission]?</b>: see which users have a particular permission<br/>\
        &nbsp; <b>can I [permission]?</b>: check if you have a specific permission<br/>\
        <b>Admin Functions</b></br>\
        &nbsp; <b>grant [nick] permission to [permission]</b>: grant [nick] a permission (requires the 'grant_permissions' permission)<br/>\
        &nbsp; <b>revoke [permission] from [nick]</b>: remove [nick]'s permission (requires the 'revoke_permissions' permission)", html=True)


    @respond_to("^(?:show permissions for|what can) (?P<nick>\w+)(?: do|)")
    def show_user_permission(self, message, nick):
        """what can [nick] do: get permissions for a user"""
        self.reply(message, "working on that...")
        if 'i' == nick.lower():
            nick = message.sender.nick
        user = User.get_from_nick(self, nick)
        if not user:
            self.reply(message, "I could not find user '{}'".format(nick))
        else:
            self.reply(message, "User '{}' has permissions {}".format(user.nick, ', '.join(user.permissions)))

    @respond_to("^who can (?P<permission>([\w.:-]+))")
    def get_users_for_permissions(self, message, permission): 
        """who can [permission]?: see which users have a particular permission"""
        self.reply(message, "working on that...")
        nicks_with_perm = []
        for user in User.list(self):
            if user.has_permission(permission):
                nicks_with_perm.append(user.nick)

        if len(nicks_with_perm) > 0:
            self.reply(message, "The following users have permission to '{}': {}".format(permission, ', '.join(nicks_with_perm)))
        else:
            self.reply(message, "No users have permission to '{}'".format(permission))

    @respond_to("^can I(?P<permissions>( [\w.:-]+)+)")
    def confirm_user_permission(self, message, permissions): 
        """can I [permission]?: check if you have a specific permission"""
        self.reply(message, "working on that...")
        user = User.get_from_message(self, message)
        if not user:
            self.reply(message, "You have not set up two-factor authentication.  Say 'twofactor me' to set it up.")
            return

        for permission in permissions.split():
            if user.has_permission(permission):
                self.reply(message, "Yes, you can {}".format(permission))
            else:
                self.reply(message, "No, you can't {}".format(permission))

    @respond_to("^(?:give|grant) (?P<nick>\w+) permission(?P<permissions>( [\w.:-]+)+)")
    @requires_permission('grant_permissions')
    def give_user_permission(self, message, nick, permissions):
        """grant [nick] permission to [permission]: grant [nick] a permission (requires the 'grant_permissions' permission)"""
        self.reply(message, "working on that...")
        requested_permissions = permissions.split()
        try:
            requested_permissions.remove("to")
        except ValueError:
            pass

        if len(requested_permissions) == 0:
            self.reply(message, 'At least one permission must be specified')
            return

        user = User.get_from_nick(self, nick)
        if not user:
            self.reply(message, "The user has not setup two-factor authentication, please have them do so before modifying permissions")
            return

        user.grant_permissions(requested_permissions)
        user.save()

        self.reply(message, "New permissions for '{}' are: {} ".format(nick, ', '.join(user.permissions)))

    @respond_to("^(?:revoke|take away) (?P<permissions>(\w+ )+)from (?P<nick>\w+)")
    @requires_permission('revoke_permissions')
    def remove_user_permission(self, message, nick, permissions):
        """revoke [permission] from [nick]: remove [nick]'s permission (requires the 'revoke_permissions' permission)"""
        self.reply(message, "working on that...")
        requested_permissions = permissions.split()
        try:
            requested_permissions.remove("to")
            requested_permissions.remove("permission")
        except ValueError:
            pass

        if len(requested_permissions) == 0:
            self.reply(message, 'At least one permission must be specified')
            return

        user = User.get_from_nick(self, nick)
        if not user:
            self.reply(message, "The user '{}' has not setup two-factor authentication, please have them do so before modifying permissions")
            return

        user.revoke_permissions(requested_permissions)
        user.save()

        self.reply(message, "New permissions for '{}' are: {} ".format(nick, ', '.join(user.permissions)))
