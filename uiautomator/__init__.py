#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Python wrapper for Android uiautomator tool."""

from __future__ import absolute_import, print_function

import sys
import os
import functools
import subprocess
import time
import itertools
import json
import hashlib
import socket
import re
import collections
import xml.dom.minidom

import requests

from uiautomator.adb import Adb

DEVICE_PORT = int(os.environ.get('UIAUTOMATOR_DEVICE_PORT', '9008'))
LOCAL_PORT = int(os.environ.get('UIAUTOMATOR_LOCAL_PORT', '9008'))
DEBUG = os.getenv('UIAUTOMATOR_DEBUG') == 'true'
httpsession = requests.Session() # use HTTP Keep-Alive to speed request

if 'localhost' not in os.environ.get('no_proxy', ''):
    os.environ['no_proxy'] = "localhost,%s" % os.environ.get('no_proxy', '')

__author__ = "Xiaocong He, Codeskyblue"
__all__ = ["Device", "rect", "point", "Selector", "JsonRPCError"]


def debug_print(*args):
    if DEBUG:
        print(args)


def _is_windows():
    return os.name == "nt"


def U(x):
    if sys.version_info.major == 2:
        return x.decode('utf-8') if type(x) is str else x
    elif sys.version_info.major == 3:
        return x


_listeners = {}

def add_listener(name, fn):
    _listeners[name] = fn

def remove_listener(name):
    _listeners.pop(name, None)


def wrapped_param_to_property(instance, *props, **kwprops):
    def wrap_func(fn):
        @functools.wraps(fn)
        def _inner(*args, **kwargs):
            # before hooks
            for hook_func in _listeners.values():
                hook_func(dict(name=fn.__name__, self=instance, args=args, kwargs=kwargs, is_before=True))
            ret = fn(*args, **kwargs)
            # after hooks
            for hook_func in _listeners.values():
                hook_func(dict(name=fn.__name__, self=instance, args=args, kwargs=kwargs, is_before=False, retval=ret))
            return ret
        return _inner
    
    if props and kwprops:
        raise SyntaxError("Can not set both props and kwprops at the same time.")

    class _Wrapper(object):
        def __init__(self, func):
            self.func = wrap_func(func)
            self.kwargs, self.args = {}, []

        def __getattr__(self, attr):
            if kwprops:
                for prop_name, prop_values in kwprops.items():
                    if attr in prop_values and prop_name not in self.kwargs:
                        self.kwargs[prop_name] = attr
                        return self
            elif attr in props:
                self.args.append(attr)
                return self
            raise AttributeError("%s parameter is duplicated or not allowed!" % attr)

        def __call__(self, *args, **kwargs):
            if kwprops:
                kwargs.update(self.kwargs)
                self.kwargs = {}
                return self.func(*args, **kwargs)
            else:
                new_args, self.args = self.args + list(args), []
                return self.func(*new_args, **kwargs)
    return _Wrapper


def param_to_property(*props, **kwprops):
    """
    Usage:

    @property
    def open(self):
        '''
        Open notification or quick settings.
        Usage:
        d.open.notification()
        d.open.quick_settings()
        '''
        @param_to_property(action=["notification", "quick_settings"])
        def _open(action):
            if action == "notification":
                pass
            elif action == "quick_settings":
                pass
        return _open
    """
    if props and kwprops:
        raise SyntaxError("Can not set both props and kwprops at the same time.")

    class Wrapper(object):
        def __init__(self, func):
            self.func = func
            self.kwargs, self.args = {}, []

        def __getattr__(self, attr):
            if kwprops:
                for prop_name, prop_values in kwprops.items():
                    if attr in prop_values and prop_name not in self.kwargs:
                        self.kwargs[prop_name] = attr
                        return self
            elif attr in props:
                self.args.append(attr)
                return self
            raise AttributeError("%s parameter is duplicated or not allowed!" % attr)

        def __call__(self, *args, **kwargs):
            if kwprops:
                kwargs.update(self.kwargs)
                self.kwargs = {}
                return self.func(*args, **kwargs)
            else:
                new_args, self.args = self.args + list(args), []
                return self.func(*new_args, **kwargs)
    return Wrapper


class JsonRPCError(Exception):

    def __init__(self, code, message):
        self.code = int(code)
        self.message = message

    def __str__(self):
        return "JsonRPC Error code: %d, Message: %s" % (self.code, self.message)


class JsonRPCMethod(object):
    def __init__(self, url, method, timeout=30):
        self.url, self.method, self.timeout = url, method, timeout

    def remote_call(self, data):
        res = httpsession.post(self.url,
            headers={"Content-Type": "application/json"},
            timeout=self.timeout,
            data=json.dumps(data).encode('utf-8'))
        if res.status_code != 200:
            raise JsonRPCError(res.status_code, "HTTP Return code is not 200")
        jsonresult = res.json()
        if "error" in jsonresult and jsonresult["error"]:
            error = jsonresult['error']
            exception_type = error.get("data", {}).get("exceptionTypeName", 'Unknown')
            raise JsonRPCError(
                error["code"],
                "%s: %s" % (exception_type, error["message"])
            )
        return jsonresult["result"]
        
    def __call__(self, *args, **kwargs):
        if args and kwargs:
            raise SyntaxError("Could not accept both *args and **kwargs as JSONRPC parameters.")
        debug_print('jsonrpc method:', self.method)
        data = {"jsonrpc": "2.0", "method": self.method, "id": self.id()}
        if args:
            data["params"] = args
        elif kwargs:
            data["params"] = kwargs
        return self.remote_call(data)

    def id(self):
        m = hashlib.md5()
        m.update(("%s at %f" % (self.method, time.time())).encode("utf-8"))
        return m.hexdigest()


class JsonRPCClient(object):
    def __init__(self, url, timeout=30, method_class=JsonRPCMethod):
        self.url = url
        self.timeout = timeout
        self.method_class = method_class

    def __getattr__(self, method):
        return self.method_class(self.url, method, timeout=self.timeout)


class Selector(dict):
    """The class is to build parameters for UiSelector passed to Android device.
    """
    __fields = {
        "text": (0x01, None),  # MASK_TEXT,
        "textContains": (0x02, None),  # MASK_TEXTCONTAINS,
        "textMatches": (0x04, None),  # MASK_TEXTMATCHES,
        "textStartsWith": (0x08, None),  # MASK_TEXTSTARTSWITH,
        "className": (0x10, None),  # MASK_CLASSNAME
        "classNameMatches": (0x20, None),  # MASK_CLASSNAMEMATCHES
        "description": (0x40, None),  # MASK_DESCRIPTION
        "descriptionContains": (0x80, None),  # MASK_DESCRIPTIONCONTAINS
        "descriptionMatches": (0x0100, None),  # MASK_DESCRIPTIONMATCHES
        "descriptionStartsWith": (0x0200, None),  # MASK_DESCRIPTIONSTARTSWITH
        "checkable": (0x0400, False),  # MASK_CHECKABLE
        "checked": (0x0800, False),  # MASK_CHECKED
        "clickable": (0x1000, False),  # MASK_CLICKABLE
        "longClickable": (0x2000, False),  # MASK_LONGCLICKABLE,
        "scrollable": (0x4000, False),  # MASK_SCROLLABLE,
        "enabled": (0x8000, False),  # MASK_ENABLED,
        "focusable": (0x010000, False),  # MASK_FOCUSABLE,
        "focused": (0x020000, False),  # MASK_FOCUSED,
        "selected": (0x040000, False),  # MASK_SELECTED,
        "packageName": (0x080000, None),  # MASK_PACKAGENAME,
        "packageNameMatches": (0x100000, None),  # MASK_PACKAGENAMEMATCHES,
        "resourceId": (0x200000, None),  # MASK_RESOURCEID,
        "resourceIdMatches": (0x400000, None),  # MASK_RESOURCEIDMATCHES,
        "index": (0x800000, 0),  # MASK_INDEX,
        "instance": (0x01000000, 0)  # MASK_INSTANCE,
    }
    __mask, __childOrSibling, __childOrSiblingSelector = "mask", "childOrSibling", "childOrSiblingSelector"

    def __init__(self, **kwargs):
        super(Selector, self).__setitem__(self.__mask, 0)
        super(Selector, self).__setitem__(self.__childOrSibling, [])
        super(Selector, self).__setitem__(self.__childOrSiblingSelector, [])
        for k in kwargs:
            self[k] = kwargs[k]

    def __setitem__(self, k, v):
        if k in self.__fields:
            super(Selector, self).__setitem__(U(k), U(v))
            super(Selector, self).__setitem__(self.__mask, self[self.__mask] | self.__fields[k][0])
        else:
            raise ReferenceError("%s is not allowed." % k)

    def __delitem__(self, k):
        if k in self.__fields:
            super(Selector, self).__delitem__(k)
            super(Selector, self).__setitem__(self.__mask, self[self.__mask] & ~self.__fields[k][0])

    def clone(self):
        kwargs = dict((k, self[k]) for k in self
                      if k not in [self.__mask, self.__childOrSibling, self.__childOrSiblingSelector])
        selector = Selector(**kwargs)
        for v in self[self.__childOrSibling]:
            selector[self.__childOrSibling].append(v)
        for s in self[self.__childOrSiblingSelector]:
            selector[self.__childOrSiblingSelector].append(s.clone())
        return selector

    def child(self, **kwargs):
        self[self.__childOrSibling].append("child")
        self[self.__childOrSiblingSelector].append(Selector(**kwargs))
        return self

    def sibling(self, **kwargs):
        self[self.__childOrSibling].append("sibling")
        self[self.__childOrSiblingSelector].append(Selector(**kwargs))
        return self

    child_selector, from_parent = child, sibling


def rect(top=0, left=0, bottom=100, right=100):
    return {"top": top, "left": left, "bottom": bottom, "right": right}


def intersect(rect1, rect2):
    top = rect1["top"] if rect1["top"] > rect2["top"] else rect2["top"]
    bottom = rect1["bottom"] if rect1["bottom"] < rect2["bottom"] else rect2["bottom"]
    left = rect1["left"] if rect1["left"] > rect2["left"] else rect2["left"]
    right = rect1["right"] if rect1["right"] < rect2["right"] else rect2["right"]
    return left, top, right, bottom


def point(x=0, y=0):
    return {"x": x, "y": y}

_init_local_port = LOCAL_PORT - 1


def next_local_port(adb_host=None):
    def is_port_listening(port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = s.connect_ex((str(adb_host) if adb_host else '127.0.0.1', port))
        s.close()
        return result == 0
    global _init_local_port
    _init_local_port = _init_local_port + 1 if _init_local_port < 32764 else LOCAL_PORT
    while is_port_listening(_init_local_port):
        _init_local_port += 1
    return _init_local_port


class NotFoundHandler(object):
    '''
    Handler for UI Object Not Found exception.
    It's a replacement of UiAutomator watcher on device side.
    '''

    def __init__(self):
        self.__handlers = collections.defaultdict(lambda: {'on': True, 'handlers': []})

    def __get__(self, instance, type):
        return self.__handlers[instance.adb.device_serial()]


class AutomatorServer(object):
    """start and quit rpc server on device.
    """
    __jar_files = {
        "bundle.jar": "libs/bundle.jar",
        "uiautomator-stub.jar": "libs/uiautomator-stub.jar"
    }

    __apk_files = ["libs/app-uiautomator.apk", "libs/app-uiautomator-test.apk"]
    # Used for check if installed
    # 2: fix bug
    # 3: screenrecord support
    __apk_vercode = 3
    __apk_pkgname = 'com.github.uiautomator'
    __apk_pkgname_test = 'com.github.uiautomator.test'

    __sdk = 0
    __httpsession = requests.Session() # use a standalone session

    handlers = NotFoundHandler()  # handler UI Not Found exception

    def __init__(self, serial=None, local_port=None, device_port=None, adb_server_host=None, adb_server_port=None):
        self.uiautomator_process = None
        self.adb = Adb(serial=serial, adb_server_host=adb_server_host, adb_server_port=adb_server_port)
        self.device_port = int(device_port) if device_port else DEVICE_PORT
        self.__local_port = local_port

    def get_forwarded_port(self):
        for s, lp, rp in self.adb.forward_list():
            if s == self.adb.device_serial() and rp == 'tcp:%d' % self.device_port:
                return int(lp[4:])
        return None

    @property
    def local_port(self):
        if self.__local_port:
            return self.__local_port
        for i in range(10): # Max retry 10 times
            forwarded_port = self.get_forwarded_port()
            if forwarded_port:
                self.__local_port = forwarded_port
                return self.__local_port

            port = next_local_port(self.adb.adb_server_host)
            self.adb.forward(port, self.device_port, rebind=False)
        raise RuntimeError("Error run: adb forward tcp:<any> tcp:%d" % self.device_port)

    def push(self):
        base_dir = os.path.dirname(__file__)
        for jar, url in self.__jar_files.items():
            filename = os.path.join(base_dir, url)
            self.adb.run_cmd("push", filename, "/data/local/tmp/")
        return list(self.__jar_files.keys())

    def need_install(self):
        pkginfo = self.adb.package_info(self.__apk_pkgname)
        if pkginfo is None:
            return True
        if pkginfo['version_code'] != self.__apk_vercode:
            return True
        if self.adb.package_info(self.__apk_pkgname_test) is None:
            return True
        return False
        
    def install(self):
        base_dir = os.path.dirname(__file__)
        if self.need_install():
            debug_print("install apks", self.__apk_files)
            for apk in self.__apk_files:
                self.adb.cmd("install", "-r", os.path.join(base_dir, apk)).wait()
        else:
            debug_print("already installed, skip")
    
    def uninstall(self):
        self.adb.cmd("uninstall", self.__apk_pkgname)
        self.adb.cmd("uninstall", self.__apk_pkgname_test)

    @property
    def jsonrpc(self):
        return self.jsonrpc_wrap(timeout=int(os.environ.get("jsonrpc_timeout", 90)))

    def jsonrpc_wrap(self, timeout):
        server = self
        error_code_base = -32000

        def _JsonRPCMethod(url, method, timeout, restart=True):
            _method_obj = JsonRPCMethod(url, method, timeout)
            _URLError = requests.exceptions.ConnectionError

            def wrapper(*args, **kwargs):
                try:
                    return _method_obj(*args, **kwargs)
                except (_URLError, socket.error) as e:
                    if restart:
                        debug_print('restart')
                        server.stop()
                        server.start(timeout=30)
                        return _JsonRPCMethod(url, method, timeout, False)(*args, **kwargs)
                    else:
                        raise
                except JsonRPCError as e:
                    debug_print('rpc error', e.code, e.message)
                    if 'UiAutomation not connected' in e.message:
                        self.uninstall()
                        return _JsonRPCMethod(url, method, timeout, False)(*args, **kwargs)
                    else:
                    # if e.message
                        raise
                    # if e.code >= error_code_base - 1:
                    #     server.stop()
                    #     server.start(timeout=10)
                    #     return _method_obj(*args, **kwargs)
                    # elif e.code == error_code_base - 2 and self.handlers['on']:  # Not Found
                    #     try:
                    #         self.handlers['on'] = False
                    #         # any handler returns True will break the left handlers
                    #         any(handler(self.handlers.get('device', None)) for handler in self.handlers['handlers'])
                    #     finally:
                    #         self.handlers['on'] = True
                    #     return _method_obj(*args, **kwargs)
                    # raise
            return wrapper

        return JsonRPCClient(self.rpc_uri,
                             timeout=timeout,
                             method_class=_JsonRPCMethod)

    def __jsonrpc(self):
        return JsonRPCClient(self.rpc_uri, timeout=int(os.environ.get("JSONRPC_TIMEOUT", 90)))

    def sdk_version(self):
        '''sdk version of connected device.'''
        if self.__sdk == 0:
            try:
                self.__sdk = int(self.adb.cmd("shell", "getprop", "ro.build.version.sdk").communicate()[0].decode("utf-8").strip())
            except:
                pass
        return self.__sdk

    def ro_product(self):
        return self.adb.shell("getprop", "ro.build.product").strip()

    def ro_manufacturer(self):
        return self.adb.shell("getprop", "ro.product.manufacturer").strip().lower()

    def start(self, timeout=5):
        # 对应关系列表
        # http://www.cnblogs.com/lipeineng/archive/2017/01/06/6257859.html
        # Android 4.3 (sdk=18)
        # Android 5.0 (sdk=21)
        debug_print('sdk version(instrument>=18)', self.sdk_version())
        debug_print('product', self.ro_product())
        debug_print('manufacturer', self.ro_manufacturer())
        if self.sdk_version() >= 18:
            self.install()
            cmd = ["shell", "am", "instrument", "-w", "-r",
                    "-e", "debug", "false", 
                    "-e", "class", "com.github.uiautomator.stub.Stub",
                    "com.github.uiautomator.test/android.support.test.runner.AndroidJUnitRunner"]
        else:
            files = self.push()
            cmd = list(itertools.chain(
                ["shell", "uiautomator", "runtest"],
                files,
                ["-c", "com.github.uiautomatorstub.Stub"]
            ))

        debug_print('$ ' + subprocess.list2cmdline(list(cmd)))
        self.uiautomator_process = self.adb.cmd(*cmd)
        self.adb.forward(self.local_port, self.device_port)

        while not self.alive and timeout > 0:
            time.sleep(0.2)
            timeout -= 0.2
            debug_print('poll', self.uiautomator_process.poll())
            if self.uiautomator_process.poll() is not None:
                stdout = self.uiautomator_process.stdout.read()
                raise IOError("uiautomator start failed: " + str(stdout))
        if not self.alive:
            raise IOError("RPC server not started!")

    def ping(self):
        try:
            return self.__jsonrpc().ping()
        except:
            return None

    def info(self):
        try:
            return self.__jsonrpc().deviceInfo()
        except:
            return False

    @property
    def alive(self):
        '''Check if the rpc server is alive.'''
        return self.ping() == "pong" and self.info()
        # return self.ping() == "pong"

    def stop(self):
        '''Stop the rpc server.'''
        if self.uiautomator_process and self.uiautomator_process.poll() is None:
            res = None
            try:
                res = requests.get(self.stop_uri)
                self.uiautomator_process.wait()
            except:
                self.uiautomator_process.kill()
            finally:
                if res is not None:
                    res.close()
                self.uiautomator_process = None
        try:
            out = self.adb.cmd("shell", "ps", "-C", "uiautomator").communicate()[0].decode("utf-8").strip().splitlines()
            if out:
                index = out[0].split().index("PID")
                for line in out[1:]:
                    if len(line.split()) > index:
                        self.adb.cmd("shell", "kill", "-9", line.split()[index]).wait()
        except:
            pass

    @property
    def stop_uri(self):
        return "http://%s:%d/stop" % (self.adb.adb_server_host, self.local_port)

    @property
    def rpc_uri(self):
        return "http://%s:%d/jsonrpc/0" % (self.adb.adb_server_host, self.local_port)

    @property
    def screenshot_uri(self):
        return "http://%s:%d/screenshot/0" % (self.adb.adb_server_host, self.local_port)

    def screenshot(self, filename=None, scale=1.0, quality=100):
        # since sdk version is always great 18, so no check here
        # also can not use requests.Session, this will break /jsonrpc/0 requests
        try:
            r = self.__httpsession.get(self.screenshot_uri, params=dict(scale=scale, quality=quality), timeout=30)
            if filename:
                with open(filename, 'wb') as f:
                    f.write(r.content)
                    return filename
            else:
                return r.content
        except:
            pass


class AutomatorDevice(object):

    '''uiautomator wrapper of android device'''

    __orientation = (  # device orientation
        (0, "natural", "n", 0),
        (1, "left", "l", 90),
        (2, "upsidedown", "u", 180),
        (3, "right", "r", 270)
    )
    __alias = {
        "width": "displayWidth",
        "height": "displayHeight"
    }

    def __init__(self, serial=None, local_port=None, adb_server_host=None, adb_server_port=None):
        self.server = AutomatorServer(
            serial=serial,
            local_port=local_port,
            adb_server_host=adb_server_host,
            adb_server_port=adb_server_port
        )

    def __call__(self, **kwargs):
        return AutomatorDeviceObject(self, Selector(**kwargs))

    # SeriousWarning: When info() method raise AttributeError,
    # the method __getattr__ will call info again
    # which will make code into dead loop
    #
    # I keep it commentted in order to remember
    #
    # def __getattr__(self, attr):
    #     '''alias of fields in info property.'''
    #     info = self.info
    #     if attr in info:
    #         return info[attr]
    #     elif attr in self.__alias:
    #         return info[self.__alias[attr]]
    #     else:
    #         raise AttributeError("%s attribute not found!" % attr)

    @property
    def info(self):
        '''Get the device info.'''
        return self.server.jsonrpc.deviceInfo()

    def click(self, x, y):
        '''click at arbitrary coordinates.'''
        return self.server.jsonrpc.click(x, y)

    def long_click(self, x, y):
        '''long click at arbitrary coordinates.'''
        return self.swipe(x, y, x + 1, y + 1)

    def swipe(self, sx, sy, ex, ey, steps=100):
        return self.server.jsonrpc.swipe(sx, sy, ex, ey, steps)

    def swipePoints(self, points, steps=100):
        ppoints = []
        for p in points:
            ppoints.append(p[0])
            ppoints.append(p[1])
        return self.server.jsonrpc.swipePoints(ppoints, steps)

    def drag(self, sx, sy, ex, ey, steps=100):
        '''Swipe from one point to another point.'''
        return self.server.jsonrpc.drag(sx, sy, ex, ey, steps)

    def dump(self, filename=None, compressed=True, pretty=True):
        '''dump device window and pull to local file.'''
        content = self.server.jsonrpc.dumpWindowHierarchy(compressed, None)
        if filename:
            with open(filename, "wb") as f:
                f.write(content.encode("utf-8"))
        if pretty and "\n " not in content:
            xml_text = xml.dom.minidom.parseString(content.encode("utf-8"))
            content = U(xml_text.toprettyxml(indent='  '))
        return content

    def screenshot(self, filename, scale=1.0, quality=100):
        '''take screenshot.'''
        for i in range(3):
            result = self.server.screenshot(filename, scale, quality)
            if result:
                return result
            self.info # try to launch uiautomator server
            time.sleep(.1)
        raise RuntimeError("screenshot failed")


    def freeze_rotation(self, freeze=True):
        '''freeze or unfreeze the device rotation in current status.'''
        self.server.jsonrpc.freezeRotation(freeze)

    @property
    def orientation(self):
        '''
        orienting the devie to left/right or natural.
        left/l:       rotation=90 , displayRotation=1
        right/r:      rotation=270, displayRotation=3
        natural/n:    rotation=0  , displayRotation=0
        upsidedown/u: rotation=180, displayRotation=2
        '''
        return self.__orientation[self.info["displayRotation"]][1]

    @orientation.setter
    def orientation(self, value):
        '''setter of orientation property.'''
        for values in self.__orientation:
            if value in values:
                # can not set upside-down until api level 18.
                self.server.jsonrpc.setOrientation(values[1])
                break
        else:
            raise ValueError("Invalid orientation.")

    @property
    def last_traversed_text(self):
        '''get last traversed text. used in webview for highlighted text.'''
        return self.server.jsonrpc.getLastTraversedText()

    def clear_traversed_text(self):
        '''clear the last traversed text.'''
        self.server.jsonrpc.clearLastTraversedText()

    @property
    def open(self):
        '''
        Open notification or quick settings.
        Usage:
        d.open.notification()
        d.open.quick_settings()
        '''
        @param_to_property(action=["notification", "quick_settings"])
        def _open(action):
            if action == "notification":
                return self.server.jsonrpc.openNotification()
            else:
                return self.server.jsonrpc.openQuickSettings()
        return _open

    @property
    def handlers(self):
        obj = self

        class Handlers(object):

            def on(self, fn):
                if fn not in obj.server.handlers['handlers']:
                    obj.server.handlers['handlers'].append(fn)
                obj.server.handlers['device'] = obj
                return fn

            def off(self, fn):
                if fn in obj.server.handlers['handlers']:
                    obj.server.handlers['handlers'].remove(fn)

        return Handlers()

    @property
    def watchers(self):
        obj = self

        class Watchers(list):

            def __init__(self):
                for watcher in obj.server.jsonrpc.getWatchers():
                    self.append(watcher)

            @property
            def triggered(self):
                return obj.server.jsonrpc.hasAnyWatcherTriggered()

            def remove(self, name=None):
                if name:
                    obj.server.jsonrpc.removeWatcher(name)
                else:
                    for name in self:
                        obj.server.jsonrpc.removeWatcher(name)

            def reset(self):
                obj.server.jsonrpc.resetWatcherTriggers()
                return self

            def run(self):
                obj.server.jsonrpc.runWatchers()
                return self
        return Watchers()

    def watcher(self, name):
        obj = self

        class Watcher(object):

            def __init__(self):
                self.__selectors = []

            @property
            def triggered(self):
                return obj.server.jsonrpc.hasWatcherTriggered(name)

            def remove(self):
                obj.server.jsonrpc.removeWatcher(name)

            def when(self, **kwargs):
                self.__selectors.append(Selector(**kwargs))
                return self

            def click(self, **kwargs):
                obj.server.jsonrpc.registerClickUiObjectWatcher(name, self.__selectors, Selector(**kwargs))

            @property
            def press(self):
                @param_to_property(
                    "home", "back", "left", "right", "up", "down", "center",
                    "search", "enter", "delete", "del", "recent", "volume_up",
                    "menu", "volume_down", "volume_mute", "camera", "power")
                def _press(*args):
                    obj.server.jsonrpc.registerPressKeyskWatcher(name, self.__selectors, args)
                return _press
        return Watcher()

    @property
    def press(self):
        '''
        press key via name or key code. Supported key name includes:
        home, back, left, right, up, down, center, menu, search, enter,
        delete(or del), recent(recent apps), volume_up, volume_down,
        volume_mute, camera, power.
        Usage:
        d.press.back()  # press back key
        d.press.menu()  # press home key
        d.press(89)     # press keycode
        '''
        @param_to_property(
            key=["home", "back", "left", "right", "up", "down", "center",
                 "menu", "search", "enter", "delete", "del", "recent",
                 "volume_up", "volume_down", "volume_mute", "camera", "power"]
        )
        def _press(key, meta=None):
            if isinstance(key, int):
                return self.server.jsonrpc.pressKeyCode(key, meta) if meta else self.server.jsonrpc.pressKeyCode(key)
            else:
                return self.server.jsonrpc.pressKey(str(key))
        return _press

    def wakeup(self):
        '''turn on screen in case of screen off.'''
        self.server.jsonrpc.wakeUp()

    def sleep(self):
        '''turn off screen in case of screen on.'''
        self.server.jsonrpc.sleep()

    @property
    def screen(self):
        '''
        Turn on/off screen.
        Usage:
        d.screen.on()
        d.screen.off()

        d.screen == 'on'  # Check if the screen is on, same as 'd.screenOn'
        d.screen == 'off'  # Check if the screen is off, same as 'not d.screenOn'
        '''
        devive_self = self

        class _Screen(object):
            def on(self):
                return devive_self.wakeup()

            def off(self):
                return devive_self.sleep()

            def __call__(self, action):
                if action == "on":
                    return self.on()
                elif action == "off":
                    return self.off()
                else:
                    raise AttributeError("Invalid parameter: %s" % action)

            def __eq__(self, value):
                info = devive_self.info
                if "screenOn" not in info:
                    raise EnvironmentError("Not supported on Android 4.3 and belows.")
                if value in ["on", "On", "ON"]:
                    return info["screenOn"]
                elif value in ["off", "Off", "OFF"]:
                    return not info["screenOn"]
                raise ValueError("Invalid parameter. It can only be compared with on/off.")

            def __ne__(self, value):
                return not self.__eq__(value)

        return _Screen()

    @property
    def wait(self):
        '''
        Waits for the current application to idle or window update event occurs.
        Usage:
        d.wait.idle(timeout=1000)
        d.wait.update(timeout=1000, package_name="com.android.settings")
        '''
        @param_to_property(action=["idle", "update"])
        def _wait(action, timeout=1000, package_name=None):
            if timeout / 1000 + 5 > int(os.environ.get("JSONRPC_TIMEOUT", 90)):
                http_timeout = timeout / 1000 + 5
            else:
                http_timeout = int(os.environ.get("JSONRPC_TIMEOUT", 90))
            if action == "idle":
                return self.server.jsonrpc_wrap(timeout=http_timeout).waitForIdle(timeout)
            elif action == "update":
                return self.server.jsonrpc_wrap(timeout=http_timeout).waitForWindowUpdate(package_name, timeout)
        return _wait

    def exists(self, **kwargs):
        '''Check if the specified ui object by kwargs exists.'''
        return self(**kwargs).exists

Device = AutomatorDevice


def wait_exists(fn):
    def _inner(self, *args, **kwargs):
        self.wait_for_exists(self.timeout) # default
        fn(self, *args, **kwargs)

    return _inner


class AutomatorDeviceUiObject(object):
    '''Represent a UiObject, on which user can perform actions, such as click, set text
    '''

    __alias = {'description': "contentDescription"}

    def __init__(self, device, selector):
        self.device = device
        self.jsonrpc = device.server.jsonrpc
        self.selector = selector
        self.timeout = 3000

    @property
    def exists(self):
        '''check if the object exists in current window.'''
        return self.jsonrpc.exist(self.selector)

    def __getattr__(self, attr):
        '''alias of fields in info property.'''

        info = self.info
        if attr in info:
            return info[attr]
        elif attr in self.__alias:
            return info[self.__alias[attr]]
        else:
            raise AttributeError("%s attribute not found!" % attr)

    def wait_for_exists(self, timeout=3000):
        """ Wait until exists """
        return self.jsonrpc.waitForExists(self.selector, timeout)

    @property
    def info(self):
        '''ui object info.'''
        return self.jsonrpc.objInfo(self.selector)

    @wait_exists
    def set_text(self, text):
        '''set the text field.'''
        if text in [None, ""]:
            return self.jsonrpc.clearTextField(self.selector)  # TODO no return
        else:
            return self.jsonrpc.setText(self.selector, text)

    @wait_exists
    def clear_text(self):
        '''clear text. alias for set_text(None).'''
        self.set_text(None)

    @wait_exists
    def click(self):
        '''
        click on the ui object.
        Usage:
        d(text="Clock").click()  # click on the center of the ui object
        '''
        return self.jsonrpc.click(self.selector)

    @property
    def long_click(self):
        '''
        Perform a long click action on the object.
        Usage:
        d(text="Image").long_click()  # long click on the center of the ui object
        d(text="Image").long_click.topleft()  # long click on the topleft of the ui object
        d(text="Image").long_click.bottomright()  # long click on the topleft of the ui object
        '''
        @wrapped_param_to_property(self, corner=["tl", "topleft", "br", "bottomright"])
        def _long_click(corner=None):
            info = self.info
            if info["longClickable"]:
                if corner:
                    return self.jsonrpc.longClick(self.selector, corner)
                else:
                    return self.jsonrpc.longClick(self.selector)
            else:
                bounds = info.get("visibleBounds") or info.get("bounds")
                if corner in ["tl", "topleft"]:
                    x = (5 * bounds["left"] + bounds["right"]) / 6
                    y = (5 * bounds["top"] + bounds["bottom"]) / 6
                elif corner in ["br", "bottomright"]:
                    x = (bounds["left"] + 5 * bounds["right"]) / 6
                    y = (bounds["top"] + 5 * bounds["bottom"]) / 6
                else:
                    x = (bounds["left"] + bounds["right"]) / 2
                    y = (bounds["top"] + bounds["bottom"]) / 2
                return self.device.long_click(x, y)
        return _long_click

    @property
    def drag(self):
        '''
        Drag the ui object to other point or ui object.
        Usage:
        d(text="Clock").drag.to(x=100, y=100)  # drag to point (x,y)
        d(text="Clock").drag.to(text="Remove") # drag to another object
        '''
        def to(obj, *args, **kwargs):
            if len(args) >= 2 or "x" in kwargs or "y" in kwargs:
                def drag_to(x, y, steps=100):
                    return self.jsonrpc.dragTo(self.selector, x, y, steps)
            else:
                def drag_to(steps=100, **kwargs):
                    return self.jsonrpc.dragTo(self.selector, Selector(**kwargs), steps)
            return drag_to(*args, **kwargs)
        return type("Drag", (object,), {"to": to})()

    def gesture(self, start1, start2, *args, **kwargs):
        '''
        perform two point gesture.
        Usage:
        d().gesture(startPoint1, startPoint2).to(endPoint1, endPoint2, steps)
        d().gesture(startPoint1, startPoint2, endPoint1, endPoint2, steps)
        '''
        def to(obj_self, end1, end2, steps=100):
            def ctp(pt):
                return point(*pt) if type(pt) == tuple else pt
            s1, s2, e1, e2 = ctp(start1), ctp(start2), ctp(end1), ctp(end2)
            return self.jsonrpc.gesture(self.selector, s1, s2, e1, e2, steps)
        obj = type("Gesture", (object,), {"to": to})()
        return obj if len(args) == 0 else to(None, *args, **kwargs)

    @property
    def pinch(self):
        '''
        Perform two point gesture from edge to center(in) or center to edge(out).
        Usages:
        d().pinch.In(percent=100, steps=10)
        d().pinch.Out(percent=100, steps=100)
        '''
        @param_to_property(in_or_out=["In", "Out"])
        def _pinch(in_or_out="Out", percent=100, steps=50):
            if in_or_out in ["Out", "out"]:
                return self.jsonrpc.pinchOut(self.selector, percent, steps)
            elif in_or_out in ["In", "in"]:
                return self.jsonrpc.pinchIn(self.selector, percent, steps)
        return _pinch

    @property
    def swipe(self):
        '''
        Perform swipe action. if device platform greater than API 18, percent can be used and value between 0 and 1
        Usages:
        d().swipe.right()
        d().swipe.left(steps=10)
        d().swipe.up(steps=10)
        d().swipe.down()
        d().swipe("right", steps=20)
        d().swipe("right", steps=20, percent=0.5)
        '''
        @param_to_property(direction=["up", "down", "right", "left"])
        def _swipe(direction="left", steps=10, percent=1):
            if percent == 1:
                return self.jsonrpc.swipe(self.selector, direction, steps)
            else:
                return self.jsonrpc.swipe(self.selector, direction, percent, steps)
        return _swipe

    @property
    def wait(self):
        '''
        Wait until the ui object gone or exist.
        Usage:
        d(text="Clock").wait.gone()  # wait until it's gone.
        d(text="Settings").wait.exists() # wait until it appears.
        '''
        @param_to_property(action=["exists", "gone"])
        def _wait(action, timeout=3000):
            if timeout / 1000 + 5 > int(os.environ.get("JSONRPC_TIMEOUT", 90)):
                http_timeout = timeout / 1000 + 5
            else:
                http_timeout = int(os.environ.get("JSONRPC_TIMEOUT", 90))
            method = self.device.server.jsonrpc_wrap(
                timeout=http_timeout
            ).waitUntilGone if action == "gone" else self.device.server.jsonrpc_wrap(timeout=http_timeout).waitForExists
            return method(self.selector, timeout)
        return _wait


class AutomatorDeviceNamedUiObject(AutomatorDeviceUiObject):

    def __init__(self, device, name):
        super(AutomatorDeviceNamedUiObject, self).__init__(device, name)

    def child(self, **kwargs):
        return AutomatorDeviceNamedUiObject(
            self.device,
            self.jsonrpc.getChild(self.selector, Selector(**kwargs))
        )

    def sibling(self, **kwargs):
        return AutomatorDeviceNamedUiObject(
            self.device,
            self.jsonrpc.getFromParent(self.selector, Selector(**kwargs))
        )


class AutomatorDeviceObject(AutomatorDeviceUiObject):

    '''Represent a generic UiObject/UiScrollable/UiCollection,
    on which user can perform actions, such as click, set text
    '''

    def __init__(self, device, selector):
        super(AutomatorDeviceObject, self).__init__(device, selector)

    def child(self, **kwargs):
        '''set childSelector.'''
        return AutomatorDeviceObject(
            self.device,
            self.selector.clone().child(**kwargs)
        )

    def sibling(self, **kwargs):
        '''set fromParent selector.'''
        return AutomatorDeviceObject(
            self.device,
            self.selector.clone().sibling(**kwargs)
        )

    child_selector, from_parent = child, sibling

    def child_by_text(self, txt, **kwargs):
        if "allow_scroll_search" in kwargs:
            allow_scroll_search = kwargs.pop("allow_scroll_search")
            name = self.jsonrpc.childByText(
                self.selector,
                Selector(**kwargs),
                txt,
                allow_scroll_search
            )
        else:
            name = self.jsonrpc.childByText(
                self.selector,
                Selector(**kwargs),
                txt
            )
        return AutomatorDeviceNamedUiObject(self.device, name)

    def child_by_description(self, txt, **kwargs):
        if "allow_scroll_search" in kwargs:
            allow_scroll_search = kwargs.pop("allow_scroll_search")
            name = self.jsonrpc.childByDescription(
                self.selector,
                Selector(**kwargs),
                txt,
                allow_scroll_search
            )
        else:
            name = self.jsonrpc.childByDescription(
                self.selector,
                Selector(**kwargs),
                txt
            )
        return AutomatorDeviceNamedUiObject(self.device, name)

    def child_by_instance(self, inst, **kwargs):
        return AutomatorDeviceNamedUiObject(
            self.device,
            self.jsonrpc.childByInstance(self.selector, Selector(**kwargs), inst)
        )

    @property
    def count(self):
        return self.jsonrpc.count(self.selector)

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        count = self.count
        if index >= count:
            raise IndexError()
        elif count == 1:
            return self
        else:
            selector = self.selector.clone()
            selector["instance"] = index
            return AutomatorDeviceObject(self.device, selector)

    def __iter__(self):
        obj, length = self, self.count

        class Iter(object):

            def __init__(self):
                self.index = -1

            def next(self):
                self.index += 1
                if self.index < length:
                    return obj[self.index]
                else:
                    raise StopIteration()
            __next__ = next

        return Iter()

    def right(self, **kwargs):
        def onrightof(rect1, rect2):
            left, top, right, bottom = intersect(rect1, rect2)
            return rect2["left"] - rect1["right"] if top < bottom else -1
        return self.__view_beside(onrightof, **kwargs)

    def left(self, **kwargs):
        def onleftof(rect1, rect2):
            left, top, right, bottom = intersect(rect1, rect2)
            return rect1["left"] - rect2["right"] if top < bottom else -1
        return self.__view_beside(onleftof, **kwargs)

    def up(self, **kwargs):
        def above(rect1, rect2):
            left, top, right, bottom = intersect(rect1, rect2)
            return rect1["top"] - rect2["bottom"] if left < right else -1
        return self.__view_beside(above, **kwargs)

    def down(self, **kwargs):
        def under(rect1, rect2):
            left, top, right, bottom = intersect(rect1, rect2)
            return rect2["top"] - rect1["bottom"] if left < right else -1
        return self.__view_beside(under, **kwargs)

    def __view_beside(self, onsideof, **kwargs):
        bounds = self.info["bounds"]
        min_dist, found = -1, None
        for ui in AutomatorDeviceObject(self.device, Selector(**kwargs)):
            dist = onsideof(bounds, ui.info["bounds"])
            if dist >= 0 and (min_dist < 0 or dist < min_dist):
                min_dist, found = dist, ui
        return found

    @property
    def fling(self):
        '''
        Perform fling action.
        Usage:
        d().fling()  # default vertically, forward
        d().fling.horiz.forward()
        d().fling.vert.backward()
        d().fling.toBeginning(max_swipes=100) # vertically
        d().fling.horiz.toEnd()
        '''
        @param_to_property(
            dimention=["vert", "vertically", "vertical", "horiz", "horizental", "horizentally"],
            action=["forward", "backward", "toBeginning", "toEnd"]
        )
        def _fling(dimention="vert", action="forward", max_swipes=1000):
            vertical = dimention in ["vert", "vertically", "vertical"]
            if action == "forward":
                return self.jsonrpc.flingForward(self.selector, vertical)
            elif action == "backward":
                return self.jsonrpc.flingBackward(self.selector, vertical)
            elif action == "toBeginning":
                return self.jsonrpc.flingToBeginning(self.selector, vertical, max_swipes)
            elif action == "toEnd":
                return self.jsonrpc.flingToEnd(self.selector, vertical, max_swipes)

        return _fling

    @property
    def scroll(self):
        '''
        Perfrom scroll action.
        Usage:
        d().scroll(steps=50) # default vertically and forward
        d().scroll.horiz.forward(steps=100)
        d().scroll.vert.backward(steps=100)
        d().scroll.horiz.toBeginning(steps=100, max_swipes=100)
        d().scroll.vert.toEnd(steps=100)
        d().scroll.horiz.to(text="Clock")
        '''
        def __scroll(vertical, forward, steps=50):
            method = self.jsonrpc.scrollForward if forward else self.jsonrpc.scrollBackward
            return method(self.selector, vertical, steps)

        def __scroll_to_beginning(vertical, steps=20, max_swipes=500):
            return self.jsonrpc.scrollToBeginning(self.selector, vertical, max_swipes, steps)

        def __scroll_to_end(vertical, steps=20, max_swipes=500):
            return self.jsonrpc.scrollToEnd(self.selector, vertical, max_swipes, steps)

        def __scroll_to(vertical, **kwargs):
            return self.jsonrpc.scrollTo(self.selector, Selector(**kwargs), vertical)

        @param_to_property(
            dimention=["vert", "vertically", "vertical", "horiz", "horizental", "horizentally"],
            action=["forward", "backward", "toBeginning", "toEnd", "to"])
        def _scroll(dimention="vert", action="forward", **kwargs):
            vertical = dimention in ["vert", "vertically", "vertical"]
            if action in ["forward", "backward"]:
                return __scroll(vertical, action == "forward", **kwargs)
            elif action == "toBeginning":
                return __scroll_to_beginning(vertical, **kwargs)
            elif action == "toEnd":
                return __scroll_to_end(vertical, **kwargs)
            elif action == "to":
                return __scroll_to(vertical, **kwargs)
        return _scroll
