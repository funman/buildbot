# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

import xml

from twisted.python import log
from twisted.internet import defer

from buildbot.process import buildstep
from buildbot.steps.source import Source
from buildbot.interfaces import BuildSlaveTooOldError


class SVN(Source):
    """I perform Subversion checkout/update operations."""

    name = 'svn'
    branch_placeholder = '%%BRANCH%%'

    renderables = [ 'svnurl', 'baseURL' ]

    def __init__(self, svnurl=None, baseURL=None, mode='incremental',
                 method=None, defaultBranch=None, username=None,
                 password=None, extra_args=None, keep_on_purge=None,
                 depth=None, **kwargs):

        self.svnurl = svnurl
        self.baseURL = baseURL
        self.branch = defaultBranch
        self.username = username
        self.password = password
        self.extra_args = extra_args
        self.keep_on_purge = keep_on_purge or []
        self.depth = depth
        self.method=method
        self.mode = mode
        Source.__init__(self, **kwargs)
        self.addFactoryArguments(svnurl=svnurl,
                                 baseURL=baseURL,
                                 mode=mode,
                                 method=method,
                                 defaultBranch=defaultBranch,
                                 password=password,
                                 username=username,
                                 extra_args=extra_args,
                                 keep_on_purge=keep_on_purge,
                                 depth=depth,
                                 )

        assert self.mode in ['incremental', 'full']
        assert self.method in ['clean', 'fresh', 'clobber', 'copy', 'export', None]

        if svnurl and baseURL:
            raise ValueError("you must provide exactly one of svnurl and"
                             " baseURL")

        if svnurl is None and baseURL is None:
            raise ValueError("you must privide at least one of svnurl and"
                             " baseURL")

    def startVC(self, branch, revision, patch):
        self.revision = revision
        self.method = self._getMethod()
        self.svnurl = self.getSvnUrl(branch)
        self.stdio_log = self.addLog("stdio")

        d = self.checkSvn()
        def checkInstall(svnInstalled):
            if not svnInstalled:
                raise BuildSlaveTooOldError("SVN is not installed on slave")
            return 0

        if self.mode == 'full':
            d.addCallback(self.full)
        elif self.mode == 'incremental':
            d.addCallback(self.incremental)
        d.addCallback(self.parseGotRevision)
        d.addCallback(self.finish)
        d.addErrback(self.failed)
        return d

    @defer.deferredGenerator
    def full(self, _):
        if self.method == 'clobber':
            wfd = defer.waitForDeferred(self.clobber())
            yield wfd
            wfd.getResult()
            return
        elif self.method in ['copy', 'export']:
            wfd = defer.waitForDeferred(self.copy())
            yield wfd
            wfd.getResult()
            return

        wfd = defer.waitForDeferred(self._sourcedirIsUpdatable())
        yield wfd
        updatable = wfd.getResult()
        if not updatable:
            d = self._dovccmd(['checkout', self.svnurl, '.'])
        elif self.method == 'clean':
            d = self.clean()
        elif self.method == 'fresh':
            d = self.fresh()

        wfd = defer.waitForDeferred(d)
        yield wfd
        wfd.getResult()

    def incremental(self, _):
        d = self._sourcedirIsUpdatable()
        def _cmd(updatable):
            if updatable:
                command = ['update']
            else:
                command = ['checkout', self.svnurl, '.']
            if self.revision:
                command.extend(['--revision', str(self.revision)])
            return command

        d.addCallback(_cmd)
        d.addCallback(self._dovccmd)
        return d

    @defer.deferredGenerator
    def clobber(self):
        cmd = buildstep.RemoteCommand('rmdir', {'dir': self.workdir,
                                                'logEnviron': self.logEnviron,})
        cmd.useLog(self.stdio_log, False)
        wfd = defer.waitForDeferred(
                self.runCommand(cmd))
        yield wfd
        wfd.getResult()
        if cmd.rc != 0:
            raise buildstep.BuildStepFailed()

        wfd = defer.waitForDeferred(
                self._dovccmd(['checkout', self.svnurl, '.']))
        yield wfd
        wfd.getResult()

    def fresh(self):
        d = self.purge(True)
        d.addCallback(lambda _: self._dovccmd(['update']))
        return d

    def clean(self):
        d = self.purge(False)
        d.addCallback(lambda _: self._dovccmd(['update']))
        return d

    @defer.deferredGenerator
    def copy(self):
        cmd = buildstep.RemoteCommand('rmdir', {'dir': self.workdir,
                                                'logEnviron': self.logEnviron,})
        cmd.useLog(self.stdio_log, False)
        wfd = defer.waitForDeferred(
                self.runCommand(cmd))
        yield wfd
        wfd.getResult()

        if cmd.rc != 0:
            raise buildstep.BuildStepFailed()

        # temporarily set workdir = 'source' and do an incremental checkout
        try:
            old_workdir = self.workdir
            self.workdir = 'source'
            wfd = defer.waitForDeferred(
                    self.incremental(None))
            yield wfd
            wfd.getResult()
        except: # finally doesn't work in python-2.4
            self.workdir = old_workdir
            raise
        self.workdir = old_workdir

        # if we're copying, copy; otherwise, export from source to build
        if self.method == 'copy':
            cmd = buildstep.RemoteCommand('cpdir',
                    { 'fromdir': 'source', 'todir':self.workdir,
                      'logEnviron': self.logEnviron })
        else:
            cmd = buildstep.RemoteShellCommand('',
                    ['svn', 'export', 'source', self.workdir],
                    env=self.env, logEnviron=self.logEnviron)
        cmd.useLog(self.stdio_log, False)

        wfd = defer.waitForDeferred(
                self.runCommand(cmd))
        yield wfd
        wfd.getResult()

        if cmd.rc != 0:
            raise buildstep.BuildStepFailed()

    def finish(self, res):
        d = defer.succeed(res)
        def _gotResults(results):
            self.setStatus(self.cmd, results)
            return results
        d.addCallback(_gotResults)
        d.addCallbacks(self.finished, self.checkDisconnect)
        return d

    def _dovccmd(self, command, collectStdout=False):
        if not command:
            raise ValueError("No command specified")
        command.extend(['--non-interactive', '--no-auth-cache'])
        if self.username:
            command.extend(['--username', self.username])
        if self.password:
            command.extend(['--password', self.password])
        if self.depth:
            command.extend(['--depth', self.depth])
        if self.extra_args:
            command.extend(self.extra_args)

        cmd = buildstep.RemoteShellCommand(self.workdir, ['svn'] + command,
                                           env=self.env,
                                           logEnviron=self.logEnviron,
                                           collectStdout=collectStdout)
        cmd.useLog(self.stdio_log, False)
        log.msg("Starting SVN command : svn %s" % (" ".join(command), ))
        d = self.runCommand(cmd)
        def evaluateCommand(cmd):
            if cmd.rc != 0:
                log.msg("Source step failed while running command %s" % cmd)
                raise buildstep.BuildStepFailed()
            if collectStdout:
                return cmd.stdout
            else:
                return cmd.rc
        d.addCallback(lambda _: evaluateCommand(cmd))
        return d

    def getSvnUrl(self, branch):
        ''' Compute the svn url that will be passed to the svn remote command '''
        if self.svnurl:
            return self.svnurl
        else:
            if branch is None:
                m = ("The SVN source step belonging to builder '%s' does not know "
                     "which branch to work with. This means that the change source "
                     "did not specify a branch and that defaultBranch is None." \
                     % self.build.builder.name)
                raise RuntimeError(m)

            computed = self.baseURL

            if self.branch_placeholder in self.baseURL:
                return computed.replace(self.branch_placeholder, branch)
            else:
                return computed + branch

    def _getMethod(self):
        if self.method is not None and self.mode != 'incremental':
            return self.method
        elif self.mode == 'incremental':
            return None
        elif self.method is None and self.mode == 'full':
            return 'fresh'

    def _sourcedirIsUpdatable(self):
        cmd = buildstep.RemoteCommand('stat', {'file': self.workdir + '/.svn',
                                               'logEnviron': self.logEnviron,})
        cmd.useLog(self.stdio_log, False)
        d = self.runCommand(cmd)
        def _fail(tmp):
            if cmd.rc != 0:
                return False
            return True
        d.addCallback(_fail)
        return d

    def parseGotRevision(self, _):
        cmd = buildstep.RemoteShellCommand(self.workdir, ['svnversion'],
                                           env=self.env,
                                           logEnviron=self.logEnviron,
                                           collectStdout=True)
        cmd.useLog(self.stdio_log, False)
        d = self.runCommand(cmd)
        def _setrev(_):
            stdout = cmd.stdout.strip()
            revision = stdout.rstrip('MSP')
            revision = revision.split(':')[-1]
            try:
                int(revision)
            except ValueError:
                msg =("SVN.parseGotRevision unable to parse output "
                      "of svnversion: '%s'" % stdout)
                log.msg(msg)
                raise buildstep.BuildStepFailed()

            log.msg("Got SVN revision %s" % (revision, ))
            self.setProperty('got_revision', revision, 'Source')
            return 0
        d.addCallback(lambda _: _setrev(cmd.rc))
        return d

    def purge(self, ignore_ignores):
        """Delete everything that shown up on status."""
        command = ['status', '--xml']
        if ignore_ignores:
            command.append('--no-ignore')
        d = self._dovccmd(command, collectStdout=True)
        def parseAndRemove(stdout):
            files = []
            for filename in self.getUnversionedFiles(stdout, self.keep_on_purge):
                filename = self.workdir+'/'+str(filename)
                files.append(filename)
            if len(files) == 0:
                d = defer.succeed(0)
            else:
                if not self.slaveVersionIsOlderThan('rmdir', '2.14'):
                    d = self.removeFiles(files)
                else:
                    cmd = buildstep.RemoteCommand('rmdir', {'dir': files,
                                                            'logEnviron':
                                                            self.logEnviron,})
                    cmd.useLog(self.stdio_log, False)
                    d = self.runCommand(cmd)
                    d.addCallback(lambda _: cmd.rc)
            return d
        d.addCallback(parseAndRemove)
        def evaluateCommand(rc):
            if rc != 0:
                log.msg("Failed removing files")
                raise buildstep.BuildStepFailed()
            return rc
        d.addCallback(evaluateCommand)
        return d

    @staticmethod
    def getUnversionedFiles(xmlStr, keep_on_purge):
        try:
            result_xml = xml.dom.minidom.parseString(xmlStr)
        except xml.parsers.expat.ExpatError:
            log.err("Corrupted xml, aborting step")
            raise buildstep.BuildStepFailed()

        for entry in result_xml.getElementsByTagName('entry'):
            (wc_status,) = entry.getElementsByTagName('wc-status')
            if wc_status.getAttribute('item') == 'external':
                continue
            if wc_status.getAttribute('item') == 'missing':
                continue
            filename = entry.getAttribute('path')
            if filename in keep_on_purge or filename == '':
                continue
            yield filename

    @defer.deferredGenerator
    def removeFiles(self, files):
        for filename in files:
            cmd = buildstep.RemoteCommand('rmdir', {'dir': filename,
                                                    'logEnviron': self.logEnviron,})
            cmd.useLog(self.stdio_log, False)
            wfd = defer.waitForDeferred(self.runCommand(cmd))
            yield wfd
            wfd.getResult()
            if cmd.rc != 0:
                yield cmd.rc
                return
        yield 0

    def checkSvn(self):
        cmd = buildstep.RemoteShellCommand(self.workdir, ['svn', '--version'],
                                           env=self.env,
                                           logEnviron=self.logEnviron)
        cmd.useLog(self.stdio_log, False)
        d = self.runCommand(cmd)
        def evaluate(cmd):
            if cmd.rc != 0:
                return False
            return True
        d.addCallback(lambda _: evaluate(cmd))
        return d

    def computeSourceRevision(self, changes):
        if not changes or None in [c.revision for c in changes]:
            return None
        lastChange = max([int(c.revision) for c in changes])
        return lastChange
    
