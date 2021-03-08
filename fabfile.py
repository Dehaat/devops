import logging as logger
import os
import re
import time
from contextlib import contextmanager

from fabric import Connection, task
from fabric.config import Config
from patchwork.files import exists
from termcolor import colored

from config_manager import ConfigManager
from utils import success, failure
from utils.s3 import S3

MAX_RELEASES = 3
PYTHON_VERSION = 3.7

logger.basicConfig(level=logger.INFO)

##################
# Template setup #
##################

# Each template gets uploaded at deploy time, only if their
# contents has changed, in which case, the reload command is
# also run.

templates = {
    "nginx": {
        "local_path": "deploy/nginx.conf.template",
        "remote_path": "/etc/nginx/sites-enabled/%(proj_name)s.conf",
        "reload_command": "service nginx restart",
    },
    "systemd": {
        "local_path": "deploy/systemd.conf.template",
        "remote_path": "/etc/systemd/system/gunicorn.service",
        "reload_command": "systemctl daemon-reload && service gunicorn restart",
    },
    "gunicorn_conf": {
        "local_path": "deploy/gunicorn.conf.py.template",
        "remote_path": "%(release_dir)s/gunicorn.conf.py",
    },
    "gunicorn_socket": {
        "local_path": "deploy/gunicorn.socket.template",
        "remote_path": "/etc/systemd/system/gunicorn.socket"
    }

}

######################################
# Config setup for different envs #
######################################
config = ConfigManager(config_file_path='config.yaml')
env = None


def default_config():
    global env
    env.timestamp = time.strftime('%Y%m%d_%H%M%S')
    env.proj_name = config.proj_name
    env.repo_url = config.repo_url
    env.deploy_dir = '/home/%s/%s' % (env.user, env.proj_name)
    env.cache_dir = '/tmp/cache'
    env.main_release_dir = os.path.join(env.deploy_dir, 'releases')
    env.current_version_dir = os.path.join(env.deploy_dir, 'current')
    env.release_dir = os.path.join(env.main_release_dir, 'release-' + env.timestamp)

    env.current_app_path = os.path.join(env.current_version_dir, env.proj_name)

    env.shared_dir = '/home/%s/shared' % env.user
    env.venv_home = "/home/%s/venvs" % env.user
    env.venv_path = os.path.join(env.venv_home, env.proj_name)

    env.proj_path = env.current_version_dir


######################################
# Context for virtualenv and project #
######################################

@contextmanager
def project(conn):
    """
    Runs commands within the project's directory.
    """

    with conn.cd(env.current_version_dir):
        yield conn


@contextmanager
def virtualenv(conn):
    """
    Runs commands within the project's virtualenv.
    """
    with conn.prefix("source %s/bin/activate" % env.venv_path):
        yield conn


@task
def prod(ctx):
    global env
    env = config.production
    env.environment = 'production'
    default_config()


@task
def staging(ctx):
    global env
    env = config.staging
    env.environment = 'staging'
    default_config()


@task
def setup(ctx, branch='master'):
    """
    Installs the base system and Python requirements for the entire server.
    """
    try:
        for host in env.hosts:
            install_system_requirements(host)
            setup_release(host, branch)
            update_env(host)
            cleanup_old_releases(host)
            upload_templates(host)
            migrate_and_create_symlink(host)
            collect_static(host)
            restart_services(host)
        print(colored(success, 'green'))
    except Exception as e:
        print(colored(failure, 'red'))
        raise e


@task
def deploy(ctx, branch='master'):
    try:
        for host in env.hosts:
            setup_release(host, branch)
            update_env(host)
            cleanup_old_releases(host)
            migrate_and_create_symlink(host)
            collect_static(host)
            restart_services(host)
            print('successfully deployed {} to host {}'.format(branch, host))
        print(colored(success, 'green'))
    except Exception as e:
        print(colored(failure, 'red'))
        raise e


@task
def run(ctx, cmd):
    """
    Runs a Command to selected env i.e staging or prod
    """
    for host in env.hosts:
        with connection(host) as conn:
            conn.run(cmd)


@task
def rollback(ctx, version=1):
    try:
        for host in env.hosts:
            with connection(host) as conn:
                releases = conn.run('ls -xt {}'.format(env.main_release_dir)).stdout.split()
                if len(releases) < 2:
                    logger.error("cannot rollback")
                    quit(1)
                index = version
                last_release = releases[index]
                create_symlink(conn, os.path.join(env.main_release_dir, last_release), env.current_version_dir)
            restart_services(host)
            print('successfully rollback on host {} '.format(host))
        print(colored(success, 'green'))
    except Exception as e:
        print(colored(failure, 'red'))
        raise e


#########################
# Install and configure #
#########################

def install_system_requirements(host):
    with connection(host) as conn:
        os_type = conn.run('echo $OSTYPE')

        if os_type.stdout.strip() != 'linux-gnu':
            print('The underlying OS is not ubuntu, terminating the script')
            return

        # Install system requirements

        if conn.run('which python{}'.format(PYTHON_VERSION)).exited == 0:
            print("Python{} is already installed".format(PYTHON_VERSION))
        else:
            print('Python{} is not installed'.format(PYTHON_VERSION))
            print('Installing Python$PYTHON_VERSION on the system'.format(PYTHON_VERSION))
            conn.sudo('apt-get update -y -q')
            conn.sudo('apt-get install software-properties-common -y -q')
            conn.sudo('add-apt-repository ppa:deadsnakes/ppa')
            conn.sudo('apt install python{} -y -q'.format(PYTHON_VERSION))
            py_path = conn.run('which python{}'.format(PYTHON_VERSION)).stdout.strip()
            if py_path:
                print('python{} is successfully installed'.format(PYTHON_VERSION))
            else:
                print('failed to install python{}'.format(PYTHON_VERSION))

        # install system requirements
        print('installing system requirements')
        conn.sudo('apt install libgdal-dev libpq-dev python{}-dev gdal-bin nginx -y -q'.format(PYTHON_VERSION))

        # create log directories
        conn.run("mkdir -p /home/%s/logs" % env.user)

        # install virtual env
        conn.sudo('apt install virtualenv -y -q')
        conn.run('virtualenv --python=python{} {}'.format(PYTHON_VERSION, env.venv_path))
        conn.run('source {}/bin/activate'.format(env.venv_path))

        # install gdal
        conn.sudo('apt-get update -y -q')
        conn.sudo('apt-get install g++ -y -q')
        with virtualenv(conn) as conn:
            conn.run('export CPLUS_INCLUDE_PATH=/usr/include/gdal')
            conn.run('export CPLUS_INCLUDE_PATH=/usr/include/gdal')
            conn.run('pip install "GDAL<=$(gdal-config --version)"')


def setup_release(host, branch):
    with connection(host) as conn:
        if not exists(conn, env.deploy_dir):
            conn.run('mkdir -p ' + env.deploy_dir)

        if not exists(conn, env.main_release_dir):
            logger.info("Creating Main release dir ~/releases/")
            conn.run('mkdir -p ' + env.main_release_dir)
            conn.run('mkdir -p ' + env.release_dir)

        if exists(conn, env.cache_dir):
            # clean up cache dir if exists
            remove(conn, env.cache_dir)

        logger.info("Creating Git cache dir")
        conn.run('mkdir -p ' + env.cache_dir)
        logger.info("Cloning repo")
        clone_project(conn, branch, env.cache_dir)

        with conn.cd(env.cache_dir):
            conn.run('cp -R . {}'.format(env.release_dir))

        with conn.cd(env.release_dir):
            conn.run("mkdir -p logs")
            install_python_requirements(conn)
        upload_template_and_reload(conn, 'gunicorn_conf')


def update_env(host):
    client = S3(env.aws_access_key_id, env.aws_secret_access_key)
    filename = env.proj_name + '.env'
    client.download_file(filename, env.environment_bucket, filename)

    with connection(host) as conn:
        if not exists(conn, env.shared_dir):
            logger.info("Creating Shared dir")
            conn.run('mkdir -p ' + env.shared_dir)

        # put env file in shared_dir
        conn.put(filename, env.shared_dir)

        # create symlink to env file
        conn.run('mv {}/{}  {}/{}'.format(env.shared_dir, filename, env.shared_dir, '.env'))
        create_symlink(conn, os.path.join(env.shared_dir, '.env'), os.path.join(env.release_dir, env.proj_name, '.env'))

        # clear environment file form local
        os.remove(filename)


def restart_services(host):
    with connection(host) as conn:
        conn.sudo('service nginx restart')
        conn.sudo('service gunicorn restart')


def cleanup_old_releases(host):
    with connection(host) as conn:
        releases = conn.run('ls -x {}'.format(env.main_release_dir)).stdout.split()
        valid = [x for x in releases if re.search(r'release-(\d+)', x).groups()]
        if len(valid) > MAX_RELEASES:
            directories = list(
                map(lambda x: os.path.join(env.main_release_dir, x), list(set(valid) - set(valid[-MAX_RELEASES:]))))
            if exists(conn, env.current_version_dir):
                current_release = conn.run('readlink {}'.format(env.current_version_dir)).stdout.strip()
                if current_release in directories:
                    logger.warning('wont delete current release {} '.format(conn.host))
                    directories.remove(current_release)
            else:
                logger.info('No current release on host: {}'.format(conn.host))
            if directories:
                for directory in directories:
                    remove(conn, directory)
            else:
                logger.info('no old releases {}'.format(conn.host))


def upload_templates(host):
    with connection(host) as conn:
        upload_template_and_reload(conn, 'gunicorn_conf')
        upload_template_and_reload(conn, 'gunicorn_socket')
        upload_template_and_reload(conn, 'systemd')
        upload_template_and_reload(conn, 'nginx')


def upload_template_and_reload(conn, name):
    """
    Uploads a template only if it has changed, and if so, reload the
    related service.
    """

    template = get_templates()[name]
    local_path = template["local_path"]
    if not os.path.exists(local_path):
        project_root = os.path.dirname(os.path.abspath(__file__))
        local_path = os.path.join(project_root, local_path)
    remote_path = template["remote_path"]
    reload_command = template.get("reload_command")

    remote_data = ""
    if exists(conn, remote_path):
        remote_data = conn.run('cat {}'.format(remote_path)).stdout.strip()
    with open(local_path, "r") as f:
        local_data = f.read()
        # Escape all non-string-formatting-placeholder occurrences of '%':
        local_data = re.sub(r"%(?!\(\w+\)s)", "%%", local_data)
        local_data %= env.__dict__

    if clean(remote_data) == clean(local_data):
        return

    temp_file_name = 'parsed_template'
    with open(temp_file_name, 'w') as f:
        f.write(local_data)

    tmp_path = '/tmp/templates'
    conn.put(temp_file_name, tmp_path)
    conn.sudo('mv {} {}'.format(tmp_path, remote_path))
    os.remove(temp_file_name)
    if reload_command:
        conn.sudo(reload_command)


###########################################
# Utils and wrappers for various commands #
###########################################


def clean(s):
    return s.replace("\n", "").replace("\r", "").strip()


def create_symlink(conn, source, dst):
    logger.info("Creating the Symlink")
    conn.run('ln -sTf %s %s' % (source, dst))


def clone_project(conn, branch, path):
    """
       Clones git repo with env.proj_name
    """
    conn.run("git clone --depth 1 -b {} {} {}".format(branch, env.repo_url, path))


def install_python_requirements(conn):
    with virtualenv(conn) as conn:
        conn.run('pip install -r requirements.txt')


def migrate_database(conn):
    with conn.cd(env.release_dir):
        with virtualenv(conn) as conn:
            conn.run('python manage.py migrate')


def migrate_and_create_symlink(host):
    with connection(host) as conn:
        if env.run_migrations != False:
            migrate_database(conn)
        create_symlink(conn, env.release_dir, env.current_version_dir)


def collect_static(host):
    with connection(host) as conn:
        with virtualenv(conn) as conn:
            with project(conn) as conn:
                conn.run('python manage.py collectstatic --noinput')


def get_templates():
    """
    Returns each of the templates with env vars injected.
    """
    injected = {}
    for name, data in templates.items():
        injected[name] = dict([(k, v % env.__dict__) for k, v in data.items()])
    return injected


@contextmanager
def connection(host):
    cfg = Config(overrides={"run": {'echo': True, 'hide': False}})
    with Connection(
            host,
            user=env.user,
            connect_kwargs={"key_filename": env.key_file_name, "allow_agent": True},
            gateway=bastion_connection(),
            config=cfg,
            forward_agent=True,
    ) as conn:
        yield conn


def remove(conn, directory):
    conn.run('rm -rfv {}'.format(directory))


def bastion_connection():
    return Connection(user=env.bastion.user,
                      host=env.bastion.host,
                      port=env.bastion.port,
                      connect_kwargs={"key_filename": env.bastion.key_file_name, "allow_agent": True},
                      forward_agent=True
                      )
