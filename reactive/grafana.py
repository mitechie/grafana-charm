import six
import os
import glob
import hashlib
import datetime
import requests
import json
import subprocess
import base64
import shutil
from charmhelpers import fetch
from charmhelpers.core import host, hookenv, unitdata
from charmhelpers.core.templating import render
from charmhelpers.contrib.charmsupport import nrpe
from charms.reactive import hook, remove_state, set_state, when, when_not
from charms.reactive.helpers import any_file_changed, data_changed, is_state


try:
    import sqlite3
except ImportError:
    fetch.apt_install(['python-sqlite'])
    import sqlite3

try:
    import pbkdf2
except ImportError:
    if six.PY3:
        fetch.apt_install(['python3-pbkdf2'])
    else:
        fetch.apt_install(['python-pbkdf2'])
    import pbkdf2

SVCNAME = 'grafana-server'
GRAFANA_INI = '/etc/grafana/grafana.ini'
GRAFANA_INI_TMPL = 'grafana.ini.j2'
GRAFANA_DEPS = ['libfontconfig1']
DASHBOARDS_BACKUP_CRON = '/etc/cron.d/juju-dashboards-backup'
DASHBOARDS_BACKUP_CRON_TMPL = 'juju-dashboards-backup.j2'


@when_not('grafana.installed')
def install_packages():
    config = hookenv.config()
    install_opts = ('install_sources', 'install_keys')
    hookenv.status_set('maintenance', 'Installing grafana')
    if config.changed('install_file') and config.get('install_file', False):
        hookenv.status_set('maintenance', 'Installing deb pkgs')
        fetch.apt_install(GRAFANA_DEPS)
        pkg_file = '/tmp/grafana.deb'
        with open(pkg_file, 'wb') as f:
            r = requests.get(config.get('install_file'), stream=True)
            for block in r.iter_content(1024):
                f.write(block)
        subprocess.check_call(['dpkg', '-i', pkg_file])
    elif any(config.changed(opt) for opt in install_opts):
        hookenv.status_set('maintenance', 'Installing deb pkgs')
        packages = ['grafana']
        fetch.configure_sources(update=True)
        fetch.apt_install(packages)
    set_state('grafana.installed')
    hookenv.status_set('active', 'Completed installing grafana')


@when('grafana.installed')
@when('config.changed.install_plugins')
def install_plugins():
    plugins_dir = '/var/lib/grafana/plugins/'

    if os.path.exists(plugins_dir):
        hookenv.log('Cleaning up currently installed plugins')
        hookenv.status_set('maintenance', 'Cleaning up currently installed plugins')
        for entry in os.listdir(plugins_dir):
            entry_path = os.path.join(plugins_dir, entry)
            if os.path.isfile(entry_path):
                os.unlink(entry_path)
            elif os.path.isdir(entry_path):
                shutil.rmtree(entry_path)
        hookenv.log('Completed cleaning up grafana plugins')
        hookenv.status_set('active', 'Completed cleaning up grafana plugins')

    config = hookenv.config()
    if config.get('install_plugins', False):
        hookenv.log('Installing plugins')
        hookenv.status_set('maintenance', 'Installing plugins')
        plugin_file = '/tmp/grafana-plugin.tar.gz'
        for plugin_url in config.get('install_plugins').split(','):
            plugin_url = plugin_url.strip()
            hookenv.log('Downloading plugin: {!r}'.format(plugin_url))
            with open(plugin_file, 'wb') as f:
                r = requests.get(plugin_url, stream=True)
                for block in r.iter_content(1024):
                    f.write(block)
            hookenv.log('Extracting plugin: {!r}'.format(plugin_url))
            shutil.unpack_archive(plugin_file, plugins_dir)
        hookenv.log('Completed installing grafana plugins')
        hookenv.status_set('active', 'Completed installing grafana plugins')


@hook('upgrade-charm')
def upgrade_charm():
    hookenv.status_set('maintenance', 'Forcing package update and reconfiguration on upgrade-charm')
    remove_state('grafana.installed')
    remove_state('grafana.configured')


@hook('config-changed')
def config_changed():
    remove_state('grafana.configured')
    config = hookenv.config()
    if config.changed('dashboards_backup_schedule') or config.changed('dashboards_backup_dir'):
        remove_state('grafana.backup.configured')
    if config.changed('admin_password') or config.changed('nagios_context'):
        remove_state('grafana.admin_password.set')


def check_ports(new_port):
    kv = unitdata.kv()
    if kv.get('grafana.port') != new_port:
        hookenv.open_port(new_port)
        if kv.get('grafana.port'):  # Dont try to close non existing ports
            hookenv.close_port(kv.get('grafana.port'))
        kv.set('grafana.port', new_port)


def select_query(query, params=None):
    conn = sqlite3.connect('/var/lib/grafana/grafana.db', timeout=30)
    cur = conn.cursor()
    if params:
        return cur.execute(query, params).fetchall()
    else:
        return cur.execute(query).fetchall()


def insert_query(query, params=None):
    conn = sqlite3.connect('/var/lib/grafana/grafana.db', timeout=30)
    cur = conn.cursor()
    if params:
        cur.execute(query, params)
    else:
        cur.execute(query)
    conn.commit()


def add_backup_api_keys():
    name = 'juju-dashboards-backup'
    passwd = host.pwgen(32)
    hpasswd = hpwgen(passwd, name)
    hookenv.log('Adding backup API keys for all organizations...')
    for i in select_query('SELECT id FROM org'):
        org_id = i[0]
        if select_query('SELECT id FROM api_key WHERE org_id=? AND name=?', [org_id, name]):
            hookenv.log('API key {} in org {} already exists, skipping'.format(name, org_id))
            continue
        j = {'n': name,
             'k': passwd,
             'id': org_id}
        encoded = base64.b64encode(json.dumps(j).encode('ascii')).decode('ascii')
        stmt = 'INSERT INTO api_key (org_id, name, key, role, created, updated)' + \
               ' VALUES (?,?,?,"Viewer",?,?)'
        dtime = datetime.datetime.today().strftime("%F %T")
        params = [org_id, name, hpasswd, dtime, dtime]
        insert_query(stmt, params)

        kv = unitdata.kv()
        backup_keys = kv.get('grafana.dashboards_backup_keys', False)
        if not backup_keys:
            backup_keys = [encoded]
        else:
            backup_keys.append(encoded)
        kv.set('grafana.dashboards_backup_keys', backup_keys)

    kv = unitdata.kv()
    return kv.get('grafana.dashboards_backup_keys')


@when('grafana.installed')
@when_not('grafana.configured')
def setup_grafana():
    hookenv.status_set('maintenance', 'Configuring grafana')
    config = hookenv.config()
    settings = {'config': config}
    render(source=GRAFANA_INI_TMPL,
           target=GRAFANA_INI,
           context=settings,
           owner='root', group='grafana',
           perms=0o640,
           )
    check_ports(config.get('port'))
    set_state('grafana.configured')
    remove_state('grafana.started')
    hookenv.status_set('active', 'Completed configuring grafana')


@when('grafana.started')
@when_not('grafana.backup.configured')
def setup_backup_shedule():
    # copy script, create cronjob, ensure directory exists

    # XXX: If you add any dependencies on config items here,
    #      be sure to update config_changed() accordingly!

    config = hookenv.config()
    if config.get('dashboards_backup_schedule', False):
        hookenv.status_set('maintenance', 'Configuring grafana dashboard backup')
        hookenv.log('Setting up dashboards backup job...')
        host.rsync('files/dashboards_backup', '/usr/local/bin/dashboards_backup')
        host.mkdir(config.get('dashboards_backup_dir'))
        settings = {'schedule': config.get('dashboards_backup_schedule'),
                    'directory': config.get('dashboards_backup_dir'),
                    'backup_keys': ' '.join(add_backup_api_keys())}
        render(source=DASHBOARDS_BACKUP_CRON_TMPL,
               target=DASHBOARDS_BACKUP_CRON,
               context=settings,
               owner='root', group='root',
               perms=0o640,
               )
        hookenv.status_set('active', 'Completed configuring grafana dashboard backup')
    set_state('grafana.backup.configured')


@when('grafana.configured')
@when_not('grafana.started')
def restart_grafana():
    if not host.service_running(SVCNAME):
        msg = 'Starting {}'.format(SVCNAME)
        hookenv.status_set('maintenance', msg)
        hookenv.log(msg)
        host.service_start(SVCNAME)
    elif any_file_changed([GRAFANA_INI]) or is_state('config.changed.install_plugins'):
        msg = 'Restarting {}'.format(SVCNAME)
        hookenv.log(msg)
        hookenv.status_set('maintenance', msg)
        host.service_restart(SVCNAME)
    hookenv.status_set('active', 'Ready')
    set_state('grafana.started')
    hookenv.status_set('active', 'Started {}'.format(SVCNAME))


@when('grafana.started')
@when('nrpe-external-master.available')
def update_nrpe_config(svc):
    # python-dbus is used by check_upstart_job
    fetch.apt_install('python-dbus')
    hostname = nrpe.get_nagios_hostname()
    current_unit = nrpe.get_nagios_unit_name()
    nrpe_setup = nrpe.NRPE(hostname=hostname)
    nrpe.add_init_service_checks(nrpe_setup, SVCNAME, current_unit)
    nrpe_setup.write()

    # XXX: Update this when https://code.launchpad.net/~paulgear/charm-helpers/nrpe-service-immediate-check/+merge/300682 is merged.
    output = open('/var/lib/nagios/service-check-grafana.txt', 'w')
    cmd = '/usr/local/lib/nagios/plugins/check_exit_status.pl -s /etc/init.d/grafana'
    ret = subprocess.call(cmd.split(), stdout=output, stderr=subprocess.STDOUT)
    hookenv.log('{} returned {}'.format(cmd, ret))


@when_not('nrpe-external-master.available')
def wipe_nrpe_checks():
    checks = ['/etc/nagios/nrpe.d/check_grafana-server.cfg',
              '/var/lib/nagios/export/service__*_grafana-server.cfg']
    for check in checks:
        for f in glob.glob(check):
            if os.path.isfile(f):
                os.unlink(f)


@when('grafana.started')
@when('grafana-source.available')
def configure_sources(relation):
    sources = relation.datasources()
    if not data_changed('grafana.sources', sources):
        return
    for ds in sources:
        hookenv.log('Found datasource: {}'.format(str(ds)))
        # Ensure datasource is configured
        check_datasource(ds)


@when('grafana.started')
@when_not('grafana-source.available')
def sources_gone():
    # Last datasource gone, remove as needed
    # TODO implementation
    pass


@when('website.available')
def configure_website(website):
    website.configure(port=hookenv.config('port'))


def validate_datasources():
    """TODO: make sure datasources option is merged with
    relation data
    TODO: make sure datasources are validated
    """
    config = hookenv.config()

    if config.get('datasources', False):
        items = config['datasources'].split(',')
        if len(items) != 7:
            return False
        elif items[0] != 'prometheus' and items[2] != 'proxy':
            return False


def check_datasource(ds):
    """
    CREATE TABLE `data_source` (
    `id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL
    , `org_id` INTEGER NOT NULL
    , `version` INTEGER NOT NULL
    , `type` TEXT NOT NULL
    , `name` TEXT NOT NULL
    , `access` TEXT NOT NULL
    , `url` TEXT NOT NULL
    , `password` TEXT NULL
    , `user` TEXT NULL
    , `database` TEXT NULL
    , `basic_auth` INTEGER NOT NULL
    , `basic_auth_user` TEXT NULL
    , `basic_auth_password` TEXT NULL
    , `is_default` INTEGER NOT NULL
    , `json_data` TEXT NULL
    , `created` DATETIME NOT NULL
    , `updated` DATETIME NOT NULL
    , `with_credentials` INTEGER NOT NULL DEFAULT 0);
    INSERT INTO "data_source" VALUES(1,1,0,'prometheus','BootStack Prometheus','proxy','http://localhost:9090','','','',0,'','',1,'{}','2016-01-22 12:11:06','2016-01-22 12:11:11',0);
    """

    # ds will be similar to:
    # {'service_name': 'prometheus',
    #  'url': 'http://10.0.3.216:9090',
    #  'description': 'Juju generated source',
    #  'type': 'prometheus',
    #  'username': 'username,
    #  'password': 'password
    # }

    conn = sqlite3.connect('/var/lib/grafana/grafana.db', timeout=30)
    cur = conn.cursor()
    query = cur.execute('SELECT id, type, name, url, is_default FROM DATA_SOURCE')
    rows = query.fetchall()
    ds_name = '{} - {}'.format(ds['service_name'], ds['description'])
    print(ds_name)
    print(rows)
    for row in rows:
        if (row[1] == ds['type'] and row[2] == ds_name and row[3] == ds['url']):
            hookenv.log('Datasource already exist, updating: {}'.format(ds_name))
            stmt, values = generate_query(ds, row[4], row[0])
            print(stmt, values)
            cur.execute(stmt, values)
            conn.commit()
            conn.close()
            return
    hookenv.log('Adding new datasource: {}'.format(ds_name))
    stmt, values = generate_query(ds, 0)
    print(stmt, values)
    cur.execute(stmt, values)
    conn.commit()
    conn.close()


def generate_query(ds, is_default, id=None):
    if not id:
        stmt = 'INSERT INTO DATA_SOURCE (org_id, version, type, name' + \
               ', access, url, is_default, created, updated, basic_auth'
        if 'username' in ds and 'password' in ds:
            stmt += ', basic_auth_user, basic_auth_password)' + \
                    ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?)'
        else:
            stmt += ') VALUES (?,?,?,?,?,?,?,?,?,?)'
        dtime = datetime.datetime.today().strftime("%F %T")
        values = (1,
                  0,
                  ds['type'],
                  '{} - {}'.format(ds['service_name'], ds['description']),
                  'proxy',
                  ds['url'],
                  is_default,
                  dtime,
                  dtime)
        if 'username' in ds and 'password' in ds:
            values = values + (1, ds['username'], ds['password'])
        else:
            values = values + (0,)
    else:
        if 'username' in ds and 'password' in ds:
            stmt = 'UPDATE DATA_SOURCE SET basic_auth_user = ?, basic_auth_password = ?, basic_auth = 1'
            values = (ds['username'], ds['password'])
        else:
            stmt = 'UPDATE DATA_SOURCE SET basic_auth_user = ?, basic_auth_password = ?, basic_auth = 0'
            values = ('', '')
    return (stmt, values)


@when('grafana.started')
@when_not('grafana.admin_password.set')
def check_adminuser():
    """
    CREATE TABLE `user` (
    `id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL
    , `version` INTEGER NOT NULL
    , `login` TEXT NOT NULL
    , `email` TEXT NOT NULL
    , `name` TEXT NULL
    , `password` TEXT NULL
    , `salt` TEXT NULL
    , `rands` TEXT NULL
    , `company` TEXT NULL
    , `org_id` INTEGER NOT NULL
    , `is_admin` INTEGER NOT NULL
    , `email_verified` INTEGER NULL
    , `theme` TEXT NULL
    , `created` DATETIME NOT NULL
    , `updated` DATETIME NOT NULL
    );
    INSERT INTO "user" VALUES(1,0,'admin','root+bootstack-ps45@canonical.com','BootStack Team','309bc4e78bc60d02dc0371d9e9fa6bf9a809d5dc25c745b9e3f85c3ed49c6feccd4ffc96d1db922f4297663a209e93f7f2b6','LZeJ3nSdrC','hseJcLcnPN','',1,1,0,'light','2016-01-22 12:00:08','2016-01-22 12:02:13');
    """

    # XXX: If you add any dependencies on config items here,
    #      be sure to update config_changed() accordingly!

    config = hookenv.config()
    passwd = config.get('admin_password', False)
    if not passwd:
        passwd = host.pwgen(16)
        kv = unitdata.kv()
        kv.set('grafana.admin_password', passwd)

    try:
        stmt = "UPDATE user SET email=?, name='BootStack Team'"
        stmt += ", password=?, theme='light'"
        stmt += " WHERE id = ?"

        conn = sqlite3.connect('/var/lib/grafana/grafana.db', timeout=30)
        cur = conn.cursor()
        query = cur.execute('SELECT id, login, salt FROM user')
        for row in query.fetchall():
            if row[1] == 'admin':
                nagios_context = config.get('nagios_context', False)
                if not nagios_context:
                    nagios_context = 'UNKNOWN'
                email = 'root+%s@canonical.com' % nagios_context
                hpasswd = hpwgen(passwd, row[2])
                if hpasswd:
                    cur.execute(stmt, (email, hpasswd, row[0]))
                    conn.commit()
                    hookenv.log('[*] admin password updated on database')
                else:
                    hookenv.log('Could not update user table: hpwgen func failed')
                break
        conn.close()
    except sqlite3.OperationalError as e:
        hookenv.log('check_adminuser::sqlite3.OperationError: {}'.format(e))
        return
    set_state('grafana.admin_password.set')


def hpwgen(passwd, salt):
    hpasswd = pbkdf2.PBKDF2(passwd, salt, 10000, hashlib.sha256).hexread(50)
    return hpasswd
