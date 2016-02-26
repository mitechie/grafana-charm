import os
import glob
from charmhelpers import fetch
from charmhelpers.core import host, hookenv
from charmhelpers.core.templating import render
from charmhelpers.contrib.charmsupport import nrpe
from charms.reactive import when, when_not, set_state, remove_state
from charms.reactive.helpers import any_file_changed, data_changed

# when
#   grafana.started
#     NO -> install and/or update -> set grafana.start
#     YES -> config-changed? restart services or else noop
#
#   grafana.start (from when_not('grafana.started')
#     NO -> noop
#     YES ->
#       config-changed? render or noop
#       service running?
#         no -> start
#         yes -> config-changed? -> restart
#

@when_not('grafana.started')
def install_packages():
    hookenv.status_set('maintenance', 'Installing deb pkgs')
    packages = ['grafana']
    config = hookenv.config()
    fetch.configure_sources(update=True)
    fetch.apt_install(packages)
    hookenv.status_set('maintenance', 'Waiting for start')
    set_state('grafana.start')


@when('grafana.start')
def setup_config()
    hookenv.status_set('maintenance', 'Configuring grafana')
    if data_changed('grafana.config', hookenv.config()):
        settings = {'config': hookenv.config(),
                    }
        render(source='grafana.ini.j2',
               target='/etc/grafana/grafana.ini',
               context=settings,
               owner='root', group='grafana',
               perms=0o640,
               )

    for svc in services():
        if not host.service_running(svc):
            hookenv.log('Starting {}...'.format(svc))
            host.service_start(svc)
        if any_file_changed(['/etc/grafana/grafana.ini']):
            hookenv.log('Restarting {}, config file changed...'.format(svc))
            host.service_restart(svc)
    set_state('grafana.started')
    remove_state('grafana.start')
    hookenv.status_set('active', 'Ready')


@when('grafana.started')
def check_config():
    if data_changed('grafana.config', hookenv.config()):
        setup_grafana()  # reconfigure and restart


@when('nrpe-external-master.available')
def update_nrpe_config(svc):
    # python-dbus is used by check_upstart_job
    fetch.apt_install('python-dbus')
    hostname = nrpe.get_nagios_hostname()
    current_unit = nrpe.get_nagios_unit_name()
    nrpe_setup = nrpe.NRPE(hostname=hostname)
    nrpe.add_init_service_checks(nrpe_setup, services(), current_unit)
    nrpe_setup.write()


@when_not('nrpe-external-master.available')
def wipe_nrpe_checks():
    checks = ['/etc/nagios/nrpe.d/check_grafana-server.cfg',
              '/var/lib/nagios/export/service__*_grafana-server.cfg']
    for check in checks:
        for f in glob.glob(check):
            if os.path.isfile(f):
                os.unlink(f)


def services():
    """Used on setup_config()
    """
    svcs = ['grafana-server']
    return svcs


def validate_datasources():
    """Unused. Check datasource before loading it into DB.
    """
    config = hookenv.config()

    if config.get('datasources', False):
        items = config['datasources'].split(',')
        if len(items) != 7:
            return False
        elif items[0] != 'prometheus' and items[2] != 'proxy':
            return False
