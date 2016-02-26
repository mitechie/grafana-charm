import os
import glob
from charmhelpers import fetch
from charmhelpers.core import host, hookenv
from charmhelpers.core.templating import render
from charmhelpers.contrib.charmsupport import nrpe
from charms.reactive import when, when_not, set_state, remove_state
from charms.reactive.helpers import any_file_changed, data_changed

@when_not('grafana.started')
def setup_grafana():
    hookenv.status_set('maintenance', 'Configuring grafana')
    install_packages()
    settings = {'config': hookenv.config(),
                }
    render(source='grafana.ini.j2',
           target='/etc/grafana/grafana.ini',
           context=settings,
           )

    set_state('grafana.start')
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


def services():
    svcs = ['grafana-server']
    return svcs


@when_not('nrpe-external-master.available')
def wipe_nrpe_checks():
    checks = ['/etc/nagios/nrpe.d/check_grafana-server.cfg',
              '/var/lib/nagios/export/service__*_grafana-server.cfg']
    for check in checks:
        for f in glob.glob(check):
            if os.path.isfile(f):
                os.unlink(f)


def validate_datasources():
    config = hookenv.config()

    if config.get('datasources', False):
        items = config['datasources'].split(',')
        if len(items) != 7:
            return False
        elif items[0] != 'prometheus' and items[2] != 'proxy':
            return False


def install_packages():
    packages = ['grafana-server']
    config = hookenv.config()
    fetch.configure_sources(update=True)
    fetch.apt_install(packages)


@when('grafana.start')
def start_grafana():
    if not host.service_running('grafana-server'):
        hookenv.log('Starting grafana...')
        host.service_start('grafana-server')
        set_state('grafana.started')
    if any_file_changed(['/etc/grafana/grafana.ini']):
        hookenv.log('Restarting grafana, config file changed...')
        host.service_restart('grafana-server')
    remove_state('grafana.start')
