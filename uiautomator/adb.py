#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import

import os
import re
import six
from collections import namedtuple
import subprocess


def _is_windows():
    return os.name == "nt"

class Adb(object):

    def __init__(self, serial=None, adb_server_host=None, adb_server_port=None):
        self.__adb_cmd = None
        self.default_serial = serial if serial else os.environ.get("ANDROID_SERIAL", None)
        self.adb_server_host = str(adb_server_host if adb_server_host else 'localhost')
        self.adb_server_port = str(adb_server_port if adb_server_port else '5037')
        self.adb_host_port_options = []
        if self.adb_server_host not in ['localhost', '127.0.0.1']:
            self.adb_host_port_options += ["-H", self.adb_server_host]
        if self.adb_server_port != '5037':
            self.adb_host_port_options += ["-P", self.adb_server_port]

    def adb(self):
        if self.__adb_cmd is None:
            if "ANDROID_HOME" in os.environ:
                filename = "adb.exe" if _is_windows() else "adb"
                adb_cmd = os.path.join(os.environ["ANDROID_HOME"], "platform-tools", filename)
                if not os.path.exists(adb_cmd):
                    raise EnvironmentError(
                        "Adb not found in $ANDROID_HOME path: %s." % os.environ["ANDROID_HOME"])
            else:
                import distutils
                if "spawn" not in dir(distutils):
                    import distutils.spawn
                adb_cmd = distutils.spawn.find_executable("adb")
                if adb_cmd:
                    adb_cmd = os.path.realpath(adb_cmd)
                else:
                    raise EnvironmentError("$ANDROID_HOME environment not set.")
            self.__adb_cmd = adb_cmd
        return self.__adb_cmd

    def cmd(self, *args, **kwargs):
        '''adb command, add -s serial by default. return the subprocess.Popen object.'''
        serial = self.device_serial()
        if serial:
            if " " in serial:  # TODO how to include special chars on command line
                serial = "'%s'" % serial
            return self.raw_cmd(*["-s", serial] + list(args))
        else:
            return self.raw_cmd(*args)

    def raw_cmd(self, *args):
        '''adb command. return the subprocess.Popen object.'''
        cmd_line = [self.adb()] + self.adb_host_port_options + list(args)
        if not _is_windows():
            cmd_line = [" ".join(cmd_line)]
        devnull = subprocess.DEVNULL if six.PY3 else open(os.devnull, 'wb')
        return subprocess.Popen(cmd_line, shell=True,
                stdin=devnull, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def run_cmd(self, *args, **kwargs):
        '''Run command and wait exit
        Args:
            - args: command args

        Returns:
            (stdout, exit_code)
        
        Raises:
            IOError
        '''
        p = self.cmd(*args)
        try:
            exit_code = p.wait()
            output = p.stdout.read()
            return namedtuple('CmdReturn', ['output', 'exit_code'])(output, exit_code)
        finally:
            p.stdout.close()

    def shell(self, *args):
        '''adb command, return adb shell <args> output.'''
        args = ['shell'] + list(args)
        return self.cmd(*args).communicate()[0].decode('utf-8')

    def package_info(self, package_name):
        '''
        Return dict if package found else None, Return example
        {
            "version_code": 27,
            "version_name": "1.2.1",
        }
        '''
        out = self.shell('dumpsys', 'package', package_name)
        result = {}
        m = re.search(r'codePath=([^\s]+)', out)
        if m:
            result['code_path'] = m.group(1)
        else:
            return None

        # other attrs
        m = re.search(r'versionCode=(\d+)', out)
        if m:
            result['version_code'] = int(m.group(1))
        m = re.search(r'versionName=([^\s]+)', out)
        if m:
            result['version_name'] = m.group(1)
        return result

    def device_serial(self):
        if not self.default_serial:
            devices = self.devices()
            if devices:
                if len(devices) is 1:
                    self.default_serial = list(devices.keys())[0]
                else:
                    raise EnvironmentError("Multiple devices attached but default android serial not set.")
            else:
                raise EnvironmentError("Device not attached.")
        return self.default_serial

    def devices(self):
        '''get a dict of attached devices. key is the device serial, value is device name.'''
        out = self.raw_cmd("devices").communicate()[0].decode("utf-8")
        match = "List of devices attached"
        index = out.find(match)
        if index < 0:
            raise EnvironmentError("adb is not working.")
        return dict([s.split("\t") for s in out[index + len(match):].strip().splitlines() if s.strip()])

    def forward(self, local_port, device_port, rebind=True):
        cmd = ['forward']
        if not rebind:
            cmd.append("--no-rebind")
        cmd += ["tcp:%d" % local_port, "tcp:%d" % device_port]
        '''adb port forward. return 0 if success, else non-zero.'''
        return self.run_cmd(*cmd).exit_code

    def forward_list(self):
        '''adb forward --list'''
        version = self.version()
        if int(version[1]) <= 1 and int(version[2]) <= 0 and int(version[3]) < 32:
            raise EnvironmentError("Low adb version. need adb >= 1.0.32")
        lines = self.raw_cmd("forward", "--list").communicate()[0].decode("utf-8").strip().splitlines()
        return [line.strip().split() for line in lines]

    def version(self):
        '''adb version'''
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", self.raw_cmd("version").communicate()[0].decode("utf-8"))
        return [match.group(i) for i in range(4)]
