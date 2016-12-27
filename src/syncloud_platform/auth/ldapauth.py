import glob
import hashlib
import os
from os.path import join
import tempfile
from subprocess import check_output

import ldap

from syncloud_app import util
from syncloud_app.logger import get_logger
import time

from syncloud_platform.gaplib import fs, linux
from syncloud_platform.application.apppaths import AppPaths

ldap_user_conf_dir = 'slapd.d'
platform_user = 'platform'


class LdapAuth:
    def __init__(self, platform_config, systemctl):
        self.systemctl = systemctl
        self.log = get_logger('ldap')
        self.config = platform_config
        self.user_conf_dir = join(self.config.data_dir(), ldap_user_conf_dir)
        self.ldap_root = '{0}/openldap'.format(self.config.app_dir())

    def installed(self):
        return os.path.isdir(join(self.config.data_dir(), ldap_user_conf_dir))

    def init(self, fix_permissions=False):
        if self.installed():
            self.log.info('ldap config already initialized')
            return

        self.log.info('initializing ldap config')
        fs.makepath(self.user_conf_dir)
        init_script = '{0}/ldap/slapd.ldif'.format(self.config.config_dir())
        
        check_output(
            '{0}/sbin/slapadd -F {1} -b "cn=config" -l {2}'.format(self.ldap_root, self.user_conf_dir, init_script), shell=True)

        if fix_permissions:
            self.log.info('fixing permissions for ldap user conf')
            fs.chownpath(self.user_conf_dir, platform_user, recursive=True)

    def reset(self, user, password):

        self.systemctl.stop_service('platform-openldap')

        fs.removepath(self.user_conf_dir)

        files = glob.glob('{0}/openldap-data/*'.format(self.config.data_dir()))
        for f in files:
            os.remove(f)

        self.init(fix_permissions=True)

        self.systemctl.start_service('platform-openldap')

        fd, filename = tempfile.mkstemp()
        util.transform_file('{0}/ldap/init.ldif'.format(self.config.config_dir()), filename, {
            'user': user,
            'password': make_secret(password)
        })

        self.__init_db(filename, self.ldap_root)

        check_output('echo "root:{0}" | chpasswd'.format(password), shell=True)

    def __init_db(self, filename, ldap_root):
        success = False
        for i in range(0, 3):
            try:
                check_output('{0}/bin/ldapadd -Y EXTERNAL -H ldapi:/// -f {1}'.format(ldap_root, filename), shell=True)
                success = True
                break
            except Exception, e:
                self.log.warn(e.message)
                self.log.warn("probably ldap is still starting, will retry {0}".format(i))
                time.sleep(1)

        if not success:
            raise Exception("Unable to initialize ldap db")


def to_ldap_dc(full_domain):
    return 'dc=' + ',dc='.join(full_domain.split('.'))


def authenticate(name, password):
    conn = ldap.initialize('ldap://localhost:389')
    try:
        conn.simple_bind_s('cn={0},ou=users,dc=syncloud,dc=org'.format(name), password)
    except Exception, e:
        conn.unbind()
        if 'desc' in e.message:
            raise Exception(e.message['desc'])
        else:
            raise Exception(e.message)


#https://gist.github.com/rca/7217540
def make_secret(password):
    """
    Encodes the given password as a base64 SSHA hash+salt buffer
    """
    salt = os.urandom(4)

    # hash the password and append the salt
    sha = hashlib.sha1(password)
    sha.update(salt)

    # create a base64 encoded string of the concatenated digest + salt
    digest_salt_b64 = '{0}{1}'.format(sha.digest(), salt).encode('base64').strip()

    # now tag the digest above with the {SSHA} tag
    tagged_digest_salt = '{{SSHA}}{0}'.format(digest_salt_b64)

    return tagged_digest_salt
