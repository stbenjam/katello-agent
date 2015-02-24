#
# Copyright 2013 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#

"""
The katello virtual agent.
Provides content management APIs for pulp within the RHSM environment.
"""

import os
import sys
import httplib

sys.path.append('/usr/share/rhsm')

from yum import YumBase
from threading import Thread
from time import sleep
from logging import getLogger, Logger

from gofer.decorators import initializer, remote
from gofer.agent.plugin import Plugin
from gofer.pmon import PathMonitor
from gofer.agent.rmi import Context
from gofer.config import Config

try:
    from subscription_manager.identity import ConsumerIdentity
except ImportError:
    from subscription_manager.certlib import ConsumerIdentity

from rhsm.connection import UEPConnection, RemoteServerException

from pulp.agent.lib.dispatcher import Dispatcher
from pulp.agent.lib.conduit import Conduit as HandlerConduit


# This plugin
plugin = Plugin.find(__name__)

# Path monitoring
path_monitor = PathMonitor()

# Track registration status
registered = False


log = getLogger(__name__)


RHSM_CONFIG_PATH = '/etc/rhsm/rhsm.conf'

REPOSITORY_PATH = '/etc/yum.repos.d/redhat.repo'


@initializer
def init_plugin():
    """
    Initialize the plugin.
    Called (once) immediately after the plugin is loaded.
     - setup path monitoring.
     - setup plugin configuration.
     - send an initial repository enabled report.
    """
    path = ConsumerIdentity.certpath()
    path_monitor.add(path, certificate_changed)
    path_monitor.add(REPOSITORY_PATH, send_enabled_report)
    path_monitor.start()
    attach = Attach()
    attach.start()


def bundle(certificate):
    """
    Bundle the key and cert and write to a file.
    :param certificate: A consumer identity certificate.
    :type certificate: ConsumerIdentity
    :return: The path to written bundle.
    :rtype: str
    """
    path = os.path.join(certificate.PATH, 'bundle.pem')
    fp = open(path, 'w')
    try:
        fp.write(certificate.key)
        fp.write(certificate.cert)
        return path
    finally:
        fp.close()


def certificate_changed(path):
    """
    A certificate change has been detected.
    On registration: setup the plugin; attach to the message broker.
    On un-registration: detach from the message broker.
    :param path: The path to the file that changed.
    :type path: str
    """
    log.info('changed: %s', path)
    attach = Attach()
    attach.start()
    attach.join()


def send_enabled_report(path=REPOSITORY_PATH):
    """
    Send the enabled repository report.
    :param path: The path to a repository file.
    :type path: str
    """
    if not registered:
        return
    try:
        uep = UEP()
        certificate = ConsumerIdentity.read()
        report = EnabledReport(path)
        uep.report_enabled(certificate.getConsumerId(), report.content)
    except Exception, e:
        log.error('send enabled report failed: %s', str(e))


def update_settings():
    """
    Setup the plugin based on the RHSM configuration.
    """
    rhsm_conf = Config(RHSM_CONFIG_PATH)
    certificate = ConsumerIdentity.read()
    plugin.cfg.messaging.cacert = rhsm_conf['rhsm']['repo_ca_cert'] % rhsm_conf['rhsm']
    plugin.cfg.messaging.url = 'proton+amqps://%s:5647' % rhsm_conf['server']['hostname']
    plugin.cfg.messaging.uuid = 'pulp.agent.%s' % certificate.getConsumerId()
    bundle(certificate)


def validate_registration():
    """
    Validate consumer registration by making a REST call
    to the server.  Updates the global 'registered' variable.
    """
    global registered
    registered = False

    if ConsumerIdentity.existsAndValid():
        consumer = ConsumerIdentity.read()
        consumer_id = consumer.getConsumerId()
    else:
        return

    try:
        uep = UEP()
        consumer = uep.getConsumer(consumer_id)
        registered = (consumer is not None)
    except RemoteServerException, e:
        if e.code != httplib.NOT_FOUND:
            log.warn(str(e))
            raise
    except Exception, e:
        log.exception(str(e))
        raise


class Attach(Thread):
    """
    This thread (task) persistently:
      - validates the registration status
      - if registered, updates the plugin settings and attach.
      - if not registered, detach the plugin.
    The reason for doing this in a thread is that we don't
    want to block in the initializer.
    """

    def __init__(self):
        super(Attach, self).__init__()
        self.setDaemon(True)

    def run(self):
        while True:
            try:
                validate_registration()
                if registered:
                    send_enabled_report()
                    update_settings()
                    plugin.attach()
                else:
                    plugin.detach()
                # DONE
                break
            except Exception, e:
                log.warn(str(e))
                sleep(60)


class Conduit(HandlerConduit):
    """
    Provides integration between the gofer and pulp agent handler frameworks.
    """

    @property
    def consumer_id(self):
        """
        Get the current consumer ID
        :return: The unique consumer ID of the currently running agent
        :rtype:  str
        """
        certificate = ConsumerIdentity.read()
        return certificate.getConsumerId()

    def update_progress(self, report):
        """
        Send the updated progress report.
        :param report: A handler progress report.
        :type report: object
        """
        context = Context.current()
        context.progress.details = report
        context.progress.report()

    def cancelled(self):
        """
        Get whether the current operation has been cancelled.
        :return: True if cancelled, else False.
        :rtype: bool
        """
        context = Context.current()
        return context.cancelled()


class EnabledReport(object):
    """
    Represents the enabled repos report.
    @ivar content: The report content <dict>:
      - basearch <str>
      - releasever <str>
      - repos[] <dict>:
        - repositoryid <str>
        - baseurl <str>
    :type content: dict
    """

    @staticmethod
    def find_enabled(yb, repofn):
        """
        Get enabled repos part of the report.
        :param yb: yum lib.
        :type yb: YumBase
        :param repofn: The .repo file basename used to filter the report.
        :type repofn: str
        :return: The repo list content
        :rtype: dict
        """
        enabled = []
        for r in yb.repos.listEnabled():
            if not r.repofile:
                continue
            fn = os.path.basename(r.repofile)
            if fn != repofn:
                continue
            item = dict(repositoryid=r.id, baseurl=r.baseurl)
            enabled.append(item)
        return dict(repos=enabled)

    @staticmethod
    def generate(repofn):
        """
        Generate the report content.
        :param repofn: The .repo file basename used to filter the report.
        :type repofn: str
        :return: The report content
        :rtype: dict
        """
        yb = Yum()
        try:
            return dict(enabled_repos=EnabledReport.find_enabled(yb, repofn))
        finally:
            yb.close()

    def __init__(self, path):
        """
        :param path: A .repo file path used to filter the report.
        :type path: str
        """
        self.content = EnabledReport.generate(os.path.basename(path))

    def __str__(self):
        return str(self.content)


class Yum(YumBase):
    """
    Provides custom configured yum object.
    """

    def cleanLoggers(self):
        """
        Clean handlers leaked by yum.
        """
        for n, lg in Logger.manager.loggerDict.items():
            if not n.startswith('yum.'):
                continue
            for h in lg.handlers:
                lg.removeHandler(h)

    def close(self):
        """
        This should be handled by __del__() but YumBase
        objects never seem to completely go out of scope and
        garbage collected.
        """
        YumBase.close(self)
        self.closeRpmDB()
        self.cleanLoggers()


class UEP(UEPConnection):
    """
    Represents the UEP.
    """

    def __init__(self):
        key = ConsumerIdentity.keypath()
        cert = ConsumerIdentity.certpath()
        UEPConnection.__init__(self, key_file=key, cert_file=cert)

    def report_enabled(self, consumer_id, report):
        """
        Report enabled repositories to the UEP.
        :param consumer_id: The consumer ID.
        :type consumer_id: str
        :param report: The report to send.
        :type report: dict
        """
        log.info('reporting: %s', report)
        method = '/systems/%s/enabled_repos' % self.sanitize(consumer_id)
        return self.conn.request_put(method, report)


# --- API --------------------------------------------------------------------


class Content(object):
    """
    Pulp Content Management.
    """

    @remote
    def install(self, units, options):
        """
        Install the specified content units using the specified options.
        Delegated to content handlers.
        :param units: A list of content units to be installed.
        :type units: list of:
            { type_id:<str>, unit_key:<dict> }
        :param options: Install options; based on unit type.
        :type options: dict
        :return: A dispatch report.
        :rtype: DispatchReport
        """
        conduit = Conduit()
        dispatcher = Dispatcher()
        report = dispatcher.install(conduit, units, options)
        return report.dict()

    @remote
    def update(self, units, options):
        """
        Update the specified content units using the specified options.
        Delegated to content handlers.
        :param units: A list of content units to be updated.
        :type units: list of:
            { type_id:<str>, unit_key:<dict> }
        :param options: Update options; based on unit type.
        :type options: dict
        :return: A dispatch report.
        :rtype: DispatchReport
        """
        conduit = Conduit()
        dispatcher = Dispatcher()
        report = dispatcher.update(conduit, units, options)
        return report.dict()

    @remote
    def uninstall(self, units, options):
        """
        Uninstall the specified content units using the specified options.
        Delegated to content handlers.
        :param units: A list of content units to be uninstalled.
        :type units: list of:
            { type_id:<str>, unit_key:<dict> }
        :param options: Uninstall options; based on unit type.
        :type options: dict
        :return: A dispatch report.
        :rtype: DispatchReport
        """
        conduit = Conduit()
        dispatcher = Dispatcher()
        report = dispatcher.uninstall(conduit, units, options)
        return report.dict()
