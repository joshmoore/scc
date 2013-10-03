#!/usr/bin/env python
# -*- coding: utf-8 -*-

#
# Copyright (C) 2012-2013 University of Dundee & Open Microscopy Environment
# All Rights Reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.


import re
import os
import sys
import uuid
import subprocess
import logging
import threading
import difflib
from framework import Command, Stop

github_loaded = True
try:
    import github  # PyGithub
    try:
        github.GithubException(0, "test")
    except AttributeError:
        print >> sys.stderr, \
            "Conflicting github module. Uninstall PyGithub3"
        github_loaded = False
except ImportError, ie:
    print >> sys.stderr, \
        "Module github missing. Install via 'pip install PyGithub'"
    github_loaded = False

# Read Jenkins environment variables
jenkins_envvar = ["JOB_NAME", "BUILD_NUMBER", "BUILD_URL"]
IS_JENKINS_JOB = all([key in os.environ for key in jenkins_envvar])
if IS_JENKINS_JOB:
    JOB_NAME = os.environ.get("JOB_NAME")
    BUILD_NUMBER = os.environ.get("BUILD_NUMBER")
    BUILD_URL = os.environ.get("BUILD_URL")

#
# Public global functions
#


def hash_object(filename):
    """
    Returns the sha1 for this file using the
    same method as `git hash-object`
    """
    try:
        from hashlib import sha1 as sha_new
    except ImportError:
        from sha import new as sha_new
    digest = sha_new()
    size = os.path.getsize(filename)
    digest.update("blob %u\0" % size)
    file = open(filename, 'rb')
    length = 1024*1024
    try:
        while True:
            block = file.read(length)
            if not block:
                break
            digest.update(block)
    finally:
        file.close()
    return digest.hexdigest()


def git_config(name, user=False, local=False, value=None, config_file=None):
    dbg = logging.getLogger("scc.config").debug
    try:
        pre_cmd = ["git", "config"]
        if value is None:
            post_cmd = ["--get", name]
        else:
            post_cmd = [name, value]

        if user:
            pre_cmd.append("--global")
        elif local:
            pre_cmd.append("--local")

        if config_file is not None:
            pre_cmd.extend(["-f", config_file])

        p = subprocess.Popen(
            pre_cmd + post_cmd, stdout=subprocess.PIPE).communicate()[0]
        value = p.split("\n")[0].strip()
        if value:
            dbg("Found %s", name)
            return value
        else:
            return None
    except Exception:
        dbg("Error retrieving %s", name, exc_info=1)
        value = None
    return value


def get_token(local=False):
    """
    Get the Github API token.
    """
    return git_config("github.token", local=local)


def get_token_or_user(local=False):
    """
    Get the Github API token or the Github user if undefined.
    """
    token = get_token(local=local)
    if not token:
        token = git_config("github.user", local=local)
    return token


def get_github(login_or_token=None, password=None, **kwargs):
    """
    Create a Github instance. Can be constructed using an OAuth2 token,
    a Github login and password or anonymously.
    """
    return GHManager(login_or_token, password, **kwargs)

#
# Management classes. These allow for proper mocking in tests.
#


class GHManager(object):
    """
    By setting dont_ask to true, it's possible to prevent the call
    to getpass.getpass. This is useful during unit tests.
    """

    def __init__(self, login_or_token=None, password=None, dont_ask=False,
                 user_agent='PyGithub'):

        self.log = logging.getLogger("scc.gh")
        self.dbg = self.log.debug
        self.login_or_token = login_or_token
        self.dont_ask = dont_ask
        self.user_agent = user_agent
        try:
            self.authorize(password)
            if login_or_token or password:
                self.get_login()
        except github.GithubException, ge:
            raise Stop(ge.status, ge.data.get("message", ""))

    def exc_check_code_and_message(self, ge, status, message):
        if ge.status == status:
            msg = ge.data.get("message", "")
            if message == msg:
                return True
        return False

    def exc_is_bad_credentials(self, ge):
        return self.exc_check_code_and_message(ge, 401, "Bad credentials")

    def exc_is_not_found(self, ge):
        return self.exc_check_code_and_message(ge, 404, "Not Found")

    def authorize(self, password):
        if password is not None:
            self.create_instance(self.login_or_token, password)
        elif self.login_or_token is not None:
            try:
                self.create_instance(self.login_or_token)
                self.get_login()  # Trigger
            except github.GithubException:
                if self.dont_ask:
                    raise
                import getpass
                msg = "Enter password for http://github.com/%s:" % \
                    self.login_or_token
                try:
                    password = getpass.getpass(msg)
                    if password is not None:
                        self.create_instance(self.login_or_token, password)
                except KeyboardInterrupt:
                    raise Stop("Interrupted by the user")
        else:
            self.create_instance()

    def get_login(self):
        return self.github.get_user().login

    def get_user(self, *args):
        return self.github.get_user(*args)

    def get_organization(self, *args):
        return self.github.get_organization(*args)

    def create_instance(self, *args, **kwargs):
        """
        Subclasses can override this method in order
        to prevent use of the pygithub2 library.
        """
        self.github = github.Github(*args, user_agent=self.user_agent,
                                    **kwargs)

    def __getattr__(self, key):
        self.dbg("github.%s", key)
        return getattr(self.github, key)

    def get_rate_limiting(self):
        requests = self.github.rate_limiting
        self.dbg("Remaining requests: %s out of %s", requests[0], requests[1])

    def gh_repo(self, reponame, username=None):
        """
        Github repository are constructed by passing the user and the
        repository name as in https://github.com/username/reponame.git
        """
        if username is None:
            username = self.get_login()
        return GitHubRepository(self, username, reponame)

    def git_repo(self, path, *args, **kwargs):
        """
        Git repository instances are constructed by passing the path
        of the directory containing the repository.
        """
        return GitRepository(self, os.path.abspath(path), *args, **kwargs)

#
# Utility classes
#


class DefaultList(list):
    def __copy__(self):
        return []


class LoggerWrapper(threading.Thread):
    """
    Read text message from a pipe and redirect them
    to a logger (see python's logger module),
    the object itself is able to supply a file
    descriptor to be used for writing

    fdWrite ==> fdRead ==> pipeReader

    See:
    http://codereview.stackexchange.com/questions/6567/
    how-to-redirect-a-subprocesses-output-stdout-and-stderr-to-logging-module
    """

    def __init__(self, logger, level=logging.DEBUG):
        """
        Setup the object with a logger and a loglevel
        and start the thread
        """

        # Initialize the superclass
        threading.Thread.__init__(self)

        # Make the thread a Daemon Thread (program will exit when only daemon
        # threads are alive)
        self.daemon = True

        # Set the logger object where messages will be redirected
        self.logger = logger

        # Set the log level
        self.level = level

        # Create the pipe and store read and write file descriptors
        self.fdRead, self.fdWrite = os.pipe()

        # Create a file-like wrapper around the read file descriptor
        # of the pipe, this has been done to simplify read operations
        self.pipeReader = os.fdopen(self.fdRead)

        # Start the thread
        self.start()
    # end __init__

    def fileno(self):
        """
        Return the write file descriptor of the pipe
        """
        return self.fdWrite
    # end fileno

    def run(self):
        """
        This is the method executed by the thread, it
        simply read from the pipe (using a file-like
        wrapper) and write the text to log.
        NB the trailing newline character of the string
           read from the pipe is removed
        """

        # Endless loop, the method will exit this loop only
        # when the pipe is close that is when a call to
        # self.pipeReader.readline() returns an empty string
        while True:

            # Read a line of text from the pipe
            message_from_pipe = self.pipeReader.readline()

            # If the line read is empty the pipe has been
            # closed, do a cleanup and exit
            # WARNING: I don't know if this method is correct,
            #          further study needed
            if len(message_from_pipe) == 0:
                self.pipeReader.close()
                os.close(self.fdRead)
                return
            # end if

            # Remove the trailing newline character frm the string
            # before sending it to the logger
            if message_from_pipe[-1] == os.linesep:
                message_to_log = message_from_pipe[:-1]
            else:
                message_to_log = message_from_pipe
            # end if

            # Send the text to the logger
            self._write(message_to_log)
        # end while
    # end run

    def _write(self, message):
        """
        Utility method to send the message
        to the logger with the correct loglevel
        """
        self.logger.log(self.level, message)
    # end write


class PullRequest(object):
    def __init__(self, pull):
        """Register the Pull Request and its corresponding Issue"""
        self.log = logging.getLogger("scc.pr")
        self.dbg = self.log.debug

        self.pull = pull
        self.dbg("login = %s", self.get_login())
        self.dbg("labels = %s", self.get_labels())
        self.dbg("base = %s", self.get_base())
        self.dbg("len(comments) = %s", len(self.get_comments()))

    def __contains__(self, key):
        return key in self.get_labels()

    def __repr__(self):
        return "  # PR %s %s '%s'" % (self.get_number(), self.get_login(),
                                      self.get_title())

    def __getattr__(self, key):
        return getattr(self.pull, key)

    def parse(self, argument):

        found_body_comments = self.parse_body(argument)
        if found_body_comments:
            return found_body_comments
        else:
            found_comments = self.parse_comments(argument)
            if found_comments:
                return found_comments
            else:
                return []

    def parse_body(self, argument):
        found_comments = []
        if isinstance(argument, list):
            patterns = ["--%s" % a for a in argument]
        else:
            patterns = ["--%s" % argument]

        if self.pull.body is None:
            return found_comments

        lines = self.pull.body.splitlines()
        for line in lines:
            for pattern in patterns:
                if line.startswith(pattern):
                    found_comments.append(line.replace(pattern, ""))
        return found_comments

    def parse_comments(self, argument):
        found_comments = []
        if isinstance(argument, list):
            patterns = ["--%s" % a for a in argument]
        else:
            patterns = ["--%s" % argument]

        for comment in self.get_comments():
            lines = comment.splitlines()
            for line in lines:
                for pattern in patterns:
                    if line.startswith(pattern):
                        found_comments.append(line.replace(pattern, ""))
        return found_comments

    def get_title(self):
        """Return the title of the Pull Request."""
        return self.pull.title

    def get_user(self):
        """Return the name of the Pull Request owner."""
        return self.pull.user

    def get_login(self):
        """Return the login of the Pull Request owner."""
        return self.pull.user.login

    def get_number(self):
        """Return the number of the Pull Request."""
        return self.pull.number

    def get_issue(self):
        """Return the number of the Pull Request."""
        return self.pull.base.repo.get_issue(self.get_number())

    def get_head_login(self):
        """Return the login of the branch where the changes are implemented."""
        if self.pull.head.user:
            return self.pull.head.user.login
        # Likely an organization. E.g. head.user was missing for
        # https://github.com/openmicroscopy/ome-documentation/pull/204
        return self.pull.head.repo.owner.login

    def get_sha(self):
        """Return the SHA1 of the head of the Pull Request."""
        return self.pull.head.sha

    def get_last_commit(self, ref="base"):
        """Return the head commit of the Pull Request."""
        branch = getattr(self.pull, ref)
        return branch.repo.get_commit(self.get_sha())

    def get_base(self):
        """Return the branch against which the Pull Request is opened."""
        return self.pull.base.ref

    def get_labels(self):
        """Return the labels of the Pull Request."""
        return [x.name for x in self.get_issue().labels]

    def get_comments(self):
        """Return the labels of the Pull Request."""
        if self.get_issue().comments:
            return [comment.body for comment in
                    self.get_issue().get_comments()]
        else:
            return []

    def create_comment(self, msg):
        """Add comment to Pull Request"""

        self.get_issue().create_comment(msg)

    def edit_body(self, body):
        """Edit body of Pull Request"""

        self.pull.edit(body=body)

    def create_status(self, status, message, url):
        """Add a status to the head of the Pull Request."""
        self.get_last_commit().create_status(
            status, url or github.GithubObject.NotSet, message,
        )

    def get_last_status(self, ref="base"):
        """Return the last status of the Pull Request."""
        try:
            return self.get_last_commit(ref).get_statuses()[0]
        except IndexError:
            return None


class GitHubRepository(object):

    def __init__(self, gh, user_name, repo_name):
        self.log = logging.getLogger("scc.repo")
        self.dbg = self.log.debug
        self.gh = gh
        self.user_name = user_name
        self.repo_name = repo_name
        self.candidate_pulls = []

        try:
            self.repo = gh.get_user(user_name).get_repo(repo_name)
            if self.repo.organization:
                self.org = gh.get_organization(self.repo.organization.login)
            else:
                self.org = None
        except:
            self.log.error("Failed to find %s/%s", user_name, repo_name)
            raise

    def __repr__(self):
        return "Repository: %s/%s" % (self.user_name, self.repo_name)

    def __getattr__(self, key):
        return getattr(self.repo, key)

    def get_owner(self):
        return self.owner.login

    def is_whitelisted(self, user, default="org"):
        if default == "org":
            if self.org:
                status = self.org.has_in_public_members(user)
            else:
                status = False
        elif default == "mine":
            status = user.login == self.gh.get_login()
        elif default == "all":
            status = True
        elif default == "none":
            status = False
        else:
            raise Exception("Unknown whitelisting mode: %s", default)

        return status

    def push(self, name):
        # TODO: We need to make it possible
        # to create a GitRepository object
        # with only a remote connection for
        # just those actions which don't
        # require a clone.
        repo = "git@github.com:%s/%s.git" % (self.get_owner(), self.repo_name)
        p = subprocess.Popen(["git", "push", repo, name])
        rc = p.wait()
        if rc != 0:
            raise Exception("'git push %s %s' failed", repo, name)

    def open_pr(self, title, description, base, head):
        return self.repo.create_pull(title, description, base, head)

    def merge_info(self):
        """List the candidate Pull Request to be merged"""

        msg = "Candidate PRs:\n"
        for pullrequest in self.candidate_pulls:
            msg += str(pullrequest) + "\n"

        return msg

    def intersect(self, a, b):
        if not a or not b:
            return None

        intersection = set(a) & set(b)
        if any(intersection):
            return list(intersection)
        else:
            return None

    def run_filter(self, filters, pr_attributes, action="Include"):

        for key, value in pr_attributes.iteritems():
            intersect_set = self.intersect(filters[key], value)
            if intersect_set:
                self.dbg("# ... %s %s: %s", action, key, " ".join(value))
                return True

        return False

    def find_candidates(self, filters):
        """Find candidate Pull Requests for merging."""
        self.dbg("## PRs found:")
        msg = ""

        # Fail fast if default is none and no include filter is specified
        no_include = all(v is None for v in filters["include"].values())
        if filters["default"] == 'none' and no_include:
            return msg

        # Loop over pull requests opened aainst base
        pulls = [pull for pull in self.get_pulls()
                 if (pull.base.ref == filters["base"])]
        status_excluded_pulls = {}

        for pull in pulls:
            pullrequest = PullRequest(pull)
            pr_attributes = {}
            pr_attributes["label"] = [x.lower() for x in
                                      pullrequest.get_labels()]
            pr_attributes["user"] = [pullrequest.get_user().login]
            pr_attributes["pr"] = [str(pullrequest.get_number())]

            if not self.is_whitelisted(pullrequest.get_user(),
                                       filters["default"]):
                # Allow filter PR inclusion using include filter
                if not self.run_filter(filters["include"], pr_attributes,
                                       action="Include"):
                    continue

            # Exclude PRs specified by filters
            if self.run_filter(filters["exclude"], pr_attributes,
                               action="Exclude"):
                continue

            # Filter PRs by status if the status filter is on
            if "status" in filters and filters["status"] != "none":
                status = pullrequest.get_last_status("base")
                if status is None:
                    # If no status on the base repo, fallback on the head repo
                    status = pullrequest.get_last_status("head")

                if status is None:
                    state = ""
                else:
                    state = status.state

                exclude_1 = (filters["status"] == "success-only") and \
                    (state != "success")
                exclude_2 = (filters["status"] == "no-error") and \
                    (state in ["error", "failure"])
                if exclude_1 or exclude_2:
                    status_excluded_pulls[pullrequest] = state
                    continue

            self.dbg(pullrequest)
            self.candidate_pulls.append(pullrequest)

        if status_excluded_pulls:
            msg += "Status-excluded PRs:\n"
            for pull in status_excluded_pulls.keys():
                msg += str(pull) + " (%s)" % status_excluded_pulls[pull] + "\n"

        self.candidate_pulls.sort(lambda a, b:
                                  cmp(a.get_number(), b.get_number()))
        return msg


class GitRepository(object):

    def __init__(self, gh, path, remote="origin"):
        """
        Register the git repository path, return the current status and
        register the Github origin remote.
        """

        self.log = logging.getLogger("scc.git")
        self.dbg = self.log.debug
        self.info = self.log.info
        self.debugWrap = LoggerWrapper(self.log, logging.DEBUG)
        self.infoWrap = LoggerWrapper(self.log, logging.INFO)

        self.gh = gh
        self.cd(path)
        root_path, e = self.communicate("git", "rev-parse", "--show-toplevel")
        self.path = os.path.abspath(root_path.strip())

        self.get_status()

        # Register the remote
        [user_name, repo_name] = self.get_remote_info(remote)
        self.remote = remote
        self.submodules = []
        if gh:
            self.origin = gh.gh_repo(repo_name, user_name)

    def register_submodules(self):
        if len(self.submodules) == 0:
            for directory in self.get_submodule_paths():
                try:
                    submodule_repo = self.gh.git_repo(directory)
                    self.submodules.append(submodule_repo)
                    submodule_repo.register_submodules()
                finally:
                    self.cd(self.path)

    def cd(self, directory):
        if not os.path.abspath(os.getcwd()) == os.path.abspath(directory):
            self.dbg("cd %s", directory)
            os.chdir(directory)

    def communicate(self, *command):
        self.dbg("Calling '%s' for stdout/err" % " ".join(command))
        p = subprocess.Popen(command,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        o, e = p.communicate()
        if p.returncode:
            msg = """Failed to run '%s'
    rc:     %s
    stdout: %s
    stderr: %s""" % (" ".join(command), p.returncode, o, e)
            raise Exception(msg)
        return o, e

    def call_info(self, *command, **kwargs):
        """
        Call wrap_call with a info LoggerWrapper
        """
        return self.wrap_call(self.infoWrap, *command, **kwargs)

    def call(self, *command, **kwargs):
        """
        Call wrap_call with a debug LoggerWrapper
        """
        return self.wrap_call(self.debugWrap, *command, **kwargs)

    def call_no_wait(self, *command, **kwargs):
        """
        Call wrap_call with a debug LoggerWrapper
        """
        kwargs["no_wait"] = True
        return self.wrap_call(self.debugWrap, *command, **kwargs)

    def wrap_call(self, logWrap, *command, **kwargs):
        for x in ("stdout", "stderr"):
            if x not in kwargs:
                kwargs[x] = logWrap

        try:
            no_wait = kwargs.pop("no_wait")
        except:
            no_wait = False

        self.dbg("Calling '%s'" % " ".join(command))
        p = subprocess.Popen(command, **kwargs)
        if not no_wait:
            rc = p.wait()
            if rc:
                raise Exception("rc=%s" % rc)
        return p

    def write_directories(self):
        """Write directories in candidate PRs comments to a txt file"""

        self.cd(self.path)
        directories_log = None

        for pr in self.origin.candidate_pulls:
            directories = pr.parse_comments("test")
            if directories:
                if directories_log is None:
                    directories_log = open('directories.txt', 'w')
                for directory in directories:
                    directories_log.write(directory)
                    directories_log.write("\n")
        # Cleanup
        if directories_log:
            directories_log.close()

    #
    # General git commands
    #

    def get_current_head(self):
        """Return the symbolic name for the current branch"""
        self.cd(self.path)
        self.dbg("Get current head")
        o, e = self.communicate("git", "symbolic-ref", "HEAD")
        o = o.strip()
        refsheads = "refs/heads/"
        if o.startswith(refsheads):
            o = o[len(refsheads):]
        return o

    def get_sha1(self, branch):
        """Return the sha1 for the specified branch"""

        self.cd(self.path)
        self.dbg("Get sha1 of %s")
        o, e = self.communicate("git", "rev-parse", branch)
        return o.strip()

    def get_current_sha1(self):
        """Return the sha1 for the current commit"""

        return self.get_sha1('HEAD')

    def get_status(self):
        """Return the status of the git repository including its submodules"""
        self.cd(self.path)
        self.dbg("Check current status")
        self.call("git", "log", "--oneline", "-n", "1", "HEAD")
        self.call("git", "submodule", "status")

    def add(self, file):
        """
        Add a file to the repository. The path should
        be relative to the top of the repository.
        """
        self.cd(self.path)
        self.dbg("Adding %s...", file)
        self.call("git", "add", file)

    def commit(self, msg):
        self.cd(self.path)
        self.dbg("Committing %s...", msg)
        self.call("git", "commit", "-m", msg)

    def tag(self, tag, message=None, force=False):
        """Tag the HEAD of the git repository"""
        self.cd(self.path)
        if message is None:
            message = "Tag with version %s" % tag

        if self.has_local_tag(tag):
            raise Stop(21, "Tag %s already exists in %s." % (tag, self.path))

        if not self.is_valid_tag(tag):
            raise Stop(22, "%s is not a valid tag name." % tag)

        self.dbg("Creating tag %s...", tag)
        if force:
            self.call("git", "tag", "-f", tag, "-m", message)
        else:
            self.call("git", "tag", tag, "-m", message)

    def new_branch(self, name, head="HEAD"):
        self.cd(self.path)
        self.dbg("New branch %s from %s...", name, head)
        self.call("git", "checkout", "-b", name, head)

    def checkout_branch(self, name):
        self.cd(self.path)
        self.dbg("Checkout branch %s...", name)
        self.call("git", "checkout", name)

    def add_remote(self, name, url=None):
        self.cd(self.path)
        if url is None:
            repo_name = self.origin.repo.name
            url = "git@github.com:%s/%s.git" % (name, repo_name)
        self.dbg("Adding remote %s for %s...", name, url)
        self.call("git", "remote", "add", name, url)

    def fetch(self, remote="origin"):
        self.cd(self.path)
        self.dbg("Fetching remote %s...", remote)
        self.call("git", "fetch", remote)

    def push_branch(self, name, remote="origin", force=False):
        self.cd(self.path)
        self.dbg("Pushing branch %s to %s..." % (name, remote))
        if force:
            self.call("git", "push", "-f", remote, name)
        else:
            self.call("git", "push", remote, name)

    def delete_local_branch(self, name, force=False):
        self.cd(self.path)
        self.dbg("Deleting branch %s locally..." % name)
        d_switch = force and "-D" or "-d"
        self.call("git", "branch", d_switch, name)

    def delete_branch(self, name, remote="origin"):
        self.cd(self.path)
        self.dbg("Deleting branch %s from %s..." % (name, remote))
        self.call("git", "push", remote, ":%s" % name)

    def reset(self):
        """Reset the git repository to its HEAD"""
        self.cd(self.path)
        self.dbg("Resetting...")
        self.call("git", "reset", "--hard", "HEAD")
        self.call("git", "submodule", "update", "--recursive")

    def fast_forward(self, base, remote="origin"):
        """Execute merge --ff-only against the current base"""
        self.dbg("## Merging base to ensure closed PRs are included.")
        p = subprocess.Popen(
            ["git", "log", "--oneline", "--first-parent",
             "HEAD..%s/%s" % (remote, base)],
            stdout=subprocess.PIPE).communicate()[0].rstrip("/n")
        merge_log = p.rstrip("/n")

        p = subprocess.Popen(
            ["git", "merge", "--ff-only", "%s/%s" % (remote, base)],
            stdout=subprocess.PIPE).communicate()[0].rstrip("/n")
        msg = p.rstrip("/n").split("\n")[0] + "\n"
        self.dbg(msg)
        return msg, merge_log

    def rebase(self, newbase, upstream, sha1):
        self.call_info("git", "rebase", "--onto",
                       "%s" % newbase, "%s" % upstream, "%s" % sha1)

    def get_rev_list(self, commit):
        revlist_cmd = lambda x: ["git", "rev-list", "--first-parent", "%s" % x]
        p = subprocess.Popen(revlist_cmd(commit),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.dbg("Calling '%s'" % " ".join(revlist_cmd(commit)))
        (revlist, stderr) = p.communicate('')

        if stderr or p.returncode:
            msg = "Error output was:\n%s" % stderr
            if revlist.strip():
                msg += "Output was:\n%s" % revlist
            raise Exception(msg)

        return revlist.splitlines()

    def has_local_changes(self):
        """Check for local changes in the Git repository"""
        self.cd(self.path)
        try:
            self.call("git", "diff-index", "--quiet", "HEAD")
            self.dbg("%s has no local changes", self)
            return False
        except Exception:
            self.dbg("%s has local changes", self)
            return True

    def has_ref(self, ref):
        """Check for reference existence in the local Git repository"""

        self.cd(self.path)
        try:
            self.call("git", "show-ref", "--verify", "--quiet", ref)
            return True
        except Exception:
            return False

    def has_local_tag(self, tag):
        """Check for tag existence in the local Git repository"""

        return self.has_ref("refs/tags/%s" % tag)

    def has_local_branch(self, branch):
        """Check for branch existence in the local Git repository"""

        return self.has_ref("refs/heads/%s" % branch)

    def has_remote_branch(self, branch, remote="origin"):
        """Check for branch existence in the local Git repository"""

        return self.has_ref("refs/remotes/%s/%s" % (remote, branch))

    def has_local_object(self, commit):
        """Check for object existence in the local Git repository"""

        self.cd(self.path)
        try:
            self.call("git", "cat-file", "-e", commit)
            return True
        except Exception:
            return False

    def is_valid_tag(self, tag):
        """Check the validity of a reference name for a tag"""

        self.cd(self.path)
        try:
            self.call("git", "check-ref-format", "refs/tags/%s" % tag)
            return True
        except Exception:
            return False

    def get_submodule_paths(self):
        """Return path of repository submodules"""

        submodule_paths = self.call(
            "git", "submodule", "--quiet", "foreach", "echo $path",
            stdout=subprocess.PIPE).communicate()[0]
        submodule_paths = submodule_paths.split("\n")[:-1]

        return submodule_paths

    def merge_base(self, a, b):
        """Return the first ancestor between two branches"""

        self.cd(self.path)
        mrg, err = self.call("git", "merge-base", a, b,
                             stdout=subprocess.PIPE).communicate()
        return mrg.strip()

    #
    # Higher level git commands
    #

    def get_remote_info(self, remote_name):
        """
        Return user and repository name of the specified remote.

        Origin remote must be on Github, i.e. of type
        *github/user/repository.git
        """
        self.cd(self.path)
        config_key = "remote.%s.url" % remote_name
        originurl = git_config(config_key)
        if originurl is None:
            remotes = self.call("git", "remote", stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE).communicate()[0]
            raise Stop(1, "Failed to find remote: %s.\nAvailable remotes: %s"
                       "can be passed with the --remote argument."
                       % (remote_name, ", ".join(remotes.split("\n")[:-1])))
        if originurl[-1] == "/":
            originurl = originurl[:-1]

        # Read user from origin URL
        dirname = os.path.dirname(originurl)
        assert "github" in dirname, 'Origin URL %s is not on GitHub' % dirname
        user = os.path.basename(dirname)
        if ":" in dirname:
            user = user.split(":")[-1]

        # Read repository from origin URL
        basename = os.path.basename(originurl)
        if ".git" in basename:
            repo = basename.rsplit(".git")[0]
        else:
            repo = basename.rsplit()[0]
        return [user, repo]

    def merge(self, comment=False, commit_id="merge",
              set_commit_status=False):
        """Merge candidate pull requests."""
        self.dbg("## Unique users: %s", self.unique_logins())
        for key, url in self.remotes().items():
            self.call("git", "remote", "add", key, url)
            self.fetch(key)

        conflicting_pulls = []
        merged_pulls = []

        for pullrequest in self.origin.candidate_pulls:
            premerge_sha, e = self.call(
                "git", "rev-parse", "HEAD",
                stdout=subprocess.PIPE).communicate()
            premerge_sha = premerge_sha.rstrip("\n")

            try:
                self.call("git", "merge", "--no-ff", "-m", "%s: PR %s (%s)"
                          % (commit_id, pullrequest.get_number(),
                             pullrequest.get_title()), pullrequest.get_sha())
                merged_pulls.append(pullrequest)
            except:
                self.call("git", "reset", "--hard", "%s" % premerge_sha)
                conflicting_pulls.append(pullrequest)

                msg = "Conflicting PR."
                if IS_JENKINS_JOB:
                    msg += " Removed from build [%s#%s](%s). See the" \
                           "[console output](%s) for more details." \
                           % (JOB_NAME, BUILD_NUMBER, BUILD_URL,
                              BUILD_URL + "/consoleText")
                self.dbg(msg)

                if comment and get_token():
                    self.dbg("Adding comment to issue #%g."
                             % pullrequest.get_number())
                    pullrequest.create_comment(msg)

        merge_msg = ""
        if merged_pulls:
            merge_msg += "Merged PRs:\n"
            for merged_pull in merged_pulls:
                merge_msg += str(merged_pull) + "\n"

        if conflicting_pulls:
            merge_msg += "Conflicting PRs (not included):\n"
            for conflicting_pull in conflicting_pulls:
                merge_msg += str(conflicting_pull) + "\n"

        if set_commit_status and get_token():
            if conflicting_pulls:
                status = 'failure'
                message = 'Not all current PRs can be merged.'
            else:
                status = 'success'
                message = 'All current PRs can be merged.'
            url = BUILD_URL if IS_JENKINS_JOB else github.GithubObject.NotSet
            merge_msg += self.set_commit_status(status, message, url)

        self.call("git", "submodule", "update")
        return merge_msg

    def set_commit_status(self, status, message, url):
        msg = ""
        for pullrequest in self.origin.candidate_pulls:
            msg += "Setting commit status %s on PR %s (%s)\n" % (
                status,
                pullrequest.get_number(),
                pullrequest.get_sha(),
            )
            pullrequest.create_status(status, message, url)
        return msg

    def find_branching_point(self, topic_branch, main_branch):
        topic_revlist = self.get_rev_list(topic_branch)
        main_revlist = self.get_rev_list(main_branch)

        # Compare sequences
        s = difflib.SequenceMatcher(None, topic_revlist, main_revlist)
        matching_block = s.get_matching_blocks()
        if matching_block[0].size == 0:
            raise Exception("No matching block found")

        sha1 = main_revlist[matching_block[0].b]
        self.info("Branching SHA1: %s" % sha1[0:6])
        return sha1

    def rset_commit_status(self, filters, status, message, url, info=False):
        """Recursively set commit status for PRs for each submodule."""

        msg = ""
        msg += str(self.origin) + "\n"
        msg += self.origin.find_candidates(filters)
        if info:
            msg += self.origin.merge_info()
        else:
            msg += self.set_commit_status(status, message, url)

        for submodule_repo in self.submodules:
            submodule_name = "%s/%s" % (submodule_repo.origin.user_name,
                                        submodule_repo.origin.repo_name)

            # Create submodule filters
            import copy
            submodule_filters = copy.deepcopy(filters)

            for ftype in ["include", "exclude"]:
                if submodule_filters[ftype]["pr"]:
                    submodule_prs = [x.replace(submodule_name, '')
                                     for x in submodule_filters[ftype]["pr"]
                                     if x.startswith(submodule_name)]
                    if len(submodule_prs) > 0:
                        submodule_filters[ftype]["pr"] = submodule_prs
                    else:
                        submodule_filters[ftype]["pr"] = None

            msg += submodule_repo.rset_commit_status(
                submodule_filters, status, message, url, info)

        return msg

    def rmerge(self, filters, info=False, comment=False, commit_id="merge",
               top_message=None, update_gitmodules=False,
               set_commit_status=False):
        """Recursively merge PRs for each submodule."""

        updated = False
        merge_msg = ""
        merge_msg += str(self.origin) + "\n"
        merge_msg += self.origin.find_candidates(filters)
        if info:
            merge_msg += self.origin.merge_info()
        else:
            self.cd(self.path)
            self.write_directories()
            presha1 = self.get_current_sha1()
            ff_msg, ff_log = self.fast_forward(filters["base"],
                                               remote=self.remote)
            merge_msg += ff_msg
            # Scan the fast-forward log to produce a digest of the merged PRs
            if ff_log:
                merge_msg += "Merged PRs (fast-forward):\n"
                pattern = r'Merge pull request #(\d+)'
                for line in ff_log.split('\n'):
                    s = re.search(pattern, line)
                    if s is not None:
                        pr = self.origin.get_pull(int(s.group(1)))
                        merge_msg += str(PullRequest(pr)) + '\n'
            merge_msg += '\n'

            merge_msg += self.merge(comment, commit_id=commit_id,
                                    set_commit_status=set_commit_status)
            postsha1 = self.get_current_sha1()
            updated = (presha1 != postsha1)

        for submodule_repo in self.submodules:
            submodule_name = "%s/%s" % (submodule_repo.origin.user_name,
                                        submodule_repo.origin.repo_name)

            # Create submodule filters
            import copy
            submodule_filters = copy.deepcopy(filters)

            for ftype in ["include", "exclude"]:
                if submodule_filters[ftype]["pr"]:
                    submodule_prs = [x.replace(submodule_name, '')
                                     for x in submodule_filters[ftype]["pr"]
                                     if x.startswith(submodule_name)]
                    if len(submodule_prs) > 0:
                        submodule_filters[ftype]["pr"] = submodule_prs
                    else:
                        submodule_filters[ftype]["pr"] = None

            try:
                submodule_updated, submodule_msg = submodule_repo.rmerge(
                    submodule_filters, info, comment, commit_id=commit_id,
                    update_gitmodules=update_gitmodules,
                    set_commit_status=set_commit_status)
                merge_msg += "\n" + submodule_msg
            finally:
                self.cd(self.path)

        if IS_JENKINS_JOB:
            merge_msg_footer = "\nGenerated by %s#%s (%s)" \
                               % (JOB_NAME, BUILD_NUMBER, BUILD_URL)
        else:
            merge_msg_footer = ""

        if not info:
            if top_message is None:
                top_message = commit_id

            commit_message = "%s\n\n%s" \
                % (top_message, merge_msg + merge_msg_footer)

            if update_gitmodules:
                submodule_paths = self.get_submodule_paths()
                for path in submodule_paths:
                    # Read submodule URL registered in .gitmodules
                    config_name = "submodule.%s.url" % path
                    submodule_url = git_config(config_name,
                                               config_file=".gitmodules")

                    # Substitute submodule URL using connection login
                    user = self.gh.get_login()
                    pattern = '(.*github.com[:/]).*(/.*.git)'
                    new_url = re.sub(pattern, r'\1%s\2' % user, submodule_url)
                    git_config(config_name, config_file=".gitmodules",
                               value=new_url)

            if self.has_local_changes():
                self.call("git", "commit", "-a", "-n", "-m", commit_message)
                updated = True
        return updated, merge_msg

    def get_tag_prefix(self):
        "Return the tag prefix for this repository using git describe"

        self.cd(self.path)
        try:
            version, e = self.call("git", "describe",
                                   stdout=subprocess.PIPE).communicate()
            prefix = re.split('\d', version)[0]
        except:
            # If no tag is present on the branch, git describe fails
            prefix = ""

        return prefix

    def rtag(self, version, message=None):
        """Recursively tag repositories with a version number."""

        msg = ""
        msg += str(self.origin) + "\n"
        tag_prefix = self.get_tag_prefix()
        self.tag(tag_prefix + version, message)
        msg += "Created tag %s\n" % (tag_prefix + version)

        for submodule_repo in self.submodules:
            msg += str(submodule_repo.origin) + "\n"
            tag_prefix = submodule_repo.get_tag_prefix()
            submodule_repo.tag(tag_prefix + version, message)
            msg += "Created tag %s\n" % (tag_prefix + version)

        return msg

    def unique_logins(self):
        """Return a set of unique logins."""
        unique_logins = set()
        for pull in self.origin.candidate_pulls:
            unique_logins.add(pull.get_head_login())
        return unique_logins

    def remotes(self):
        """Return remotes associated to unique login."""
        remotes = {}
        for user in self.unique_logins():
            key = "merge_%s" % user
            if self.origin.private:
                url = "git@github.com:%s/%s.git" % (user, self.origin.name)
            else:
                url = "git://github.com/%s/%s.git" % (user, self.origin.name)
            remotes[key] = url
        return remotes

    def rcleanup(self):
        """Recursively remove remote branches created for merging."""

        self.cleanup()
        for submodule_repo in self.submodules:
            try:
                submodule_repo.rcleanup()
            except:
                self.dbg("Failed to clean repository %s" % self.path)
            self.cd(self.path)

    def cleanup(self):
        """Remove remote branches created for merging."""
        self.cd(self.path)
        if self.gh:  # no gh implies no connection
            for key in self.remotes().keys():
                try:
                    self.call("git", "remote", "rm", key)
                except Exception:
                    self.log.error("Failed to remove", key, exc_info=1)

    def rpush(self, branch_name, remote, force=False):
        """Recursively push a branch to remotes across submodules"""

        full_remote = remote % (self.origin.repo_name)
        self.push_branch(branch_name, remote=full_remote, force=force)
        self.dbg("Pushed %s to %s" % (branch_name, full_remote))

        for submodule_repo in self.submodules:
            try:
                submodule_repo.rpush(branch_name, remote, force=force)
            finally:
                self.cd(self.path)

#
# Exceptions
#


class UnknownMerge(Exception):
    """
    Exception which specifies that the given commit
    doesn't qualify as a Github-style merge.
    """

    def __init__(self, line):
        self.line = line
        super(UnknownMerge, self).__init__()


class GithubCommand(Command):
    """
    Abstract class for commands acting on a git repository
    """

    NAME = "abstract"

    def __init__(self, sub_parsers):
        super(GithubCommand, self).__init__(sub_parsers)

        sha1_chars = "^([0-9a-f]+)\s"
        self.pr_pattern = re.compile(sha1_chars +
                                     "Merge\spull\srequest\s.(\d+)\s(.*)$")
        self.commit_pattern = re.compile(sha1_chars + "(.*)$")

    def configure_logging(self, args):
        super(GithubCommand, self).configure_logging(args)
        logging.getLogger('github').setLevel(logging.INFO)

    def login(self, args):
        if args.token:
            token = args.token
        else:
            token = get_token_or_user()
        if token is None and not args.no_ask:
            print "# github.token and github.user not found."
            print "# See `%s token` for simpifying use." % sys.argv[0]
            token = raw_input("Username or token: ").strip()
        self.gh = get_github(token, dont_ask=args.no_ask)

    def parse_pr(self, line):
        m = self.pr_pattern.match(line)
        if not m:
            raise UnknownMerge(line=line)
        sha1 = m.group(1)
        num = int(m.group(2))
        rest = m.group(3)
        return sha1, num, rest

    def parse_commit(self, line):
        m = self.commit_pattern.match(line)
        if not m:
            raise UnknownMerge(line=line)
        sha1 = m.group(1)
        rest = m.group(2)
        return sha1, rest

    def add_remote_arg(self):
        self.parser.add_argument(
            '--remote', default="origin",
            help='Name of the remote to use as the origin')

    def add_token_args(self):
        self.parser.add_argument(
            "--token",
            help="Token to use rather than from config files")
        self.parser.add_argument(
            "--no-ask", action='store_true',
            help="Do not ask for a password if token usage fails")


class GitRepoCommand(GithubCommand):
    """
    Abstract class for commands acting on a git repository
    """

    NAME = "abstract"

    def __init__(self, sub_parsers):
        super(GitRepoCommand, self).__init__(sub_parsers)
        self.parser.add_argument(
            '--shallow', action='store_true',
            help='Do not recurse into submodules')
        self.parser.add_argument(
            '--reset', action='store_true',
            help='Reset the current branch to its HEAD')
        self.add_remote_arg()
        self.add_token_args()

    def init_main_repo(self, args):
        self.main_repo = self.gh.git_repo(self.cwd, remote=args.remote)
        if not args.shallow:
            self.main_repo.register_submodules()
        if args.reset:
            self.main_repo.reset()
            self.main_repo.get_status()

    def add_new_commit_args(self):
        self.parser.add_argument(
            '--message', '-m',
            help='Message to use for the commit. '
            'Overwrites auto-generated value')
        self.parser.add_argument(
            '--push', type=str,
            help='Name of the branch to use to recursively push'
            ' the merged branch to Github')
        self.parser.add_argument(
            '--update-gitmodules', action='store_true',
            help='Update submodule URLs to point at the forks'
            ' of the Github user')
        self.parser.add_argument('base', type=str)

    def push(self, args, main_repo):
        branch_name = "HEAD:refs/heads/%s" % (args.push)

        user = self.gh.get_login()
        remote = "git@github.com:%s/" % (user) + "%s.git"

        main_repo.rpush(branch_name, remote, force=True)
        gh_branch = "https://github.com/%s/%s/tree/%s" \
            % (user, main_repo.origin.repo_name, args.push)
        self.log.info("Merged branch pushed to %s" % gh_branch)

    def get_open_pr(self, args):
        user = self.gh.get_login()
        branch_name = args.push

        for pull in self.main_repo.origin.get_pulls():
            if pull.head.user.login == user and pull.head.ref == branch_name:
                self.log.info("PR %s already opened", pull.number)
                return PullRequest(pull)

        return None


class FilteredPullRequestsCommand(GitRepoCommand):
    """
    Abstract base class for repo commands that take filters to find
    and work with open pull requests
    """

    def __init__(self, sub_parsers):
        super(FilteredPullRequestsCommand, self).__init__(sub_parsers)

        filter_desc = " Filter keys can be specified using label:my_label, \
            pr:24 or  user:username. If no key is specified, the filter is \
            considered as a label filter."

        self.parser.add_argument(
            '--info', action='store_true',
            help='Display pull requests but do not perform actions on them')
        self.parser.add_argument(
            '--default', '-D', type=str,
            choices=["none", "mine", "org", "all"], default="org",
            help='Mode specifying the default PRs to include. '
            'None includes no PR. All includes all open PRs. '
            'Mine only includes the PRs opened by the authenticated user. '
            'If the repository belongs to an organization, org includes '
            'any PR opened by a public member of the organization. '
            'Default: org.')
        self.parser.add_argument(
            '--include', '-I', type=str, action='append',
            default=DefaultList(["include"]),
            help='Filters to include PRs in the merge.' + filter_desc)
        self.parser.add_argument(
            '--exclude', '-E', type=str, action='append',
            default=DefaultList(["exclude"]),
            help='Filters to exclude PRs from the merge.' + filter_desc)
        self.parser.add_argument(
            '--check-commit-status', '-S', type=str,
            choices=["none", "no-error", "success-only"], default="none",
            help='Check success/failure status on latest commits to include '
            ' PRs in the merge.')

    def _log_parse_filters(self, args, default_user):
        self.log.info("%s on PR based on %s opened by %s",
                      self.NAME, args.base, default_user)

    def _parse_filters(self, args):
        """ Read filters from arguments and fill filters dictionary"""

        self.filters = {}
        self.filters["base"] = args.base
        self.filters["default"] = args.default
        if args.default == "org":
            default_user = "any public member of the organization"
        elif args.default == "mine":
            default_user = "%s" % self.gh.get_login()
        elif args.default == "all":
            default_user = "any user"
        elif args.default == "none":
            default_user = "no user"
        else:
            raise Exception("Unknown default mode: %s", args.default)

        self._log_parse_filters(args, default_user)

        descr = {"label": " labelled as", "pr": "", "user": " opened by"}
        keys = descr.keys()
        default_key = "label"

        for ftype in ["include", "exclude"]:
            self.filters[ftype] = dict.fromkeys(keys)

            if not getattr(args, ftype):
                continue

            for filt in getattr(args, ftype):
                found = False
                for key in keys:
                    # Look for key:value pattern
                    pattern = key + ":"
                    if filt.find(pattern) == 0:
                        value = filt.replace(pattern, '', 1)
                        if self.filters[ftype][key]:
                            self.filters[ftype][key].append(value)
                        else:
                            self.filters[ftype][key] = [value]
                        found = True
                        continue

                if not found:
                    # Look for #value pattern
                    pattern = "#"
                    if filt.find(pattern) != -1:
                        value = filt.replace(pattern, '', 1)
                        if self.filters[ftype]["pr"]:
                            self.filters[ftype]["pr"].append(value)
                        else:
                            self.filters[ftype]["pr"] = [value]
                        found = True
                        continue

                if not found:
                    if self.filters[ftype][key]:
                        self.filters[ftype][default_key].append(filt)
                    else:
                        self.filters[ftype][default_key] = [filt]

            action = ftype[0].upper() + ftype[1:-1] + "ing"
            for key in keys:
                if self.filters[ftype][key]:
                    self.log.info("%s PR%s: %s", action, descr[key],
                                  " ".join(self.filters[ftype][key]))

        self.filters["status"] = args.check_commit_status
        if args.check_commit_status != "none":
            if args.check_commit_status == "success-only":
                self.log.info('Excluding PR without successful status')
            elif args.check_commit_status == "no-error":
                self.log.info('Excluding PR with error or failure status')


class CheckMilestone(GitRepoCommand):
    """Check all merged PRs for a set milestone

Find all GitHub-merged PRs between head and tag, i.e.
git log --first-parent TAG...HEAD

Usage:
    check-milestone 0.2.0 0.2.1 --set=0.2.1
    """

    NAME = "check-milestone"

    def __init__(self, sub_parsers):
        super(CheckMilestone, self).__init__(sub_parsers)
        self.parser.add_argument('tag', help="Start tag for searching")
        self.parser.add_argument('head', help="Branch to use check")
        self.parser.add_argument('--set', help="Milestone to use if unset",
                                 dest="milestone_name")

    def __call__(self, args):
        super(CheckMilestone, self).__call__(args)
        self.login(args)
        self.init_main_repo(args)
        try:

            if args.milestone_name:
                milestone = self.get_milestone(args.milestone_name)
                if not milestone:
                    raise Stop(3, "Unknown milestone: %s" %
                               args.milestone_name)

            if not self.main_repo.has_local_tag(args.tag):
                raise Stop(21, "Tag %s does not exist." % args.tag)

            o, e = self.main_repo.communicate(
                "git", "log", "--oneline", "--first-parent",
                "%s...%s" % (args.tag, args.head))

            for line in o.split("\n"):
                if line.split():
                    try:
                        sha1, num, rest = self.parse_pr(line)
                    except:
                        self.log.info("Unknown merge: %s", line)
                        continue
                    pr = self.main_repo.origin.get_issue(num)
                    if pr.milestone:
                        self.log.debug("PR %s in milestone %s",
                                       pr.number, pr.milestone.title)
                    else:
                        if args.milestone_name:
                            try:
                                pr.edit(milestone=milestone)
                                print "Set milestone for PR %s to %s" \
                                    % (pr.number, milestone.title)
                            except github.GithubException, ge:
                                if self.gh.exc_is_not_found(ge):
                                    raise Stop(10, "Can't edit milestone")
                                raise
                        else:
                            print "No milestone for PR %s ('%s')" \
                                % (pr.number, line)
        finally:
            self.main_repo.cleanup()

    def get_milestone(self, name):

        for state in ("open", "closed"):
            milestones = self.main_repo.origin.get_milestones(state=state)
            for m in milestones:
                if m.title == name:
                    return m

        return None


class AlreadyMerged(GithubCommand):
    """Detect branches local & remote which are already merged"""

    NAME = "already-merged"

    def __init__(self, sub_parsers):
        super(AlreadyMerged, self).__init__(sub_parsers)
        self.add_token_args()

        self.parser.add_argument(
            "target",
            help="Head to check against. E.g. master or origin/master")
        self.parser.add_argument(
            "ref", nargs="*",
            default=["refs/heads", "refs/remotes"],
            help="List of ref patterns to be checked. "
            "E.g. refs/remotes/origin")

    def __call__(self, args):
        super(AlreadyMerged, self).__call__(args)
        self.login(args)

        main_repo = self.gh.git_repo(self.cwd)
        try:
            self.already_merged(args, main_repo)
        finally:
            main_repo.cleanup()

    def already_merged(self, args, main_repo):
        fmt = "%(committerdate:iso8601) %(refname:short)   --- %(subject)"
        cmd = ["git", "for-each-ref", "--sort=committerdate"]
        cmd.append("--format=%s" % fmt)
        cmd += args.ref
        proc = main_repo.call(*cmd, stdout=subprocess.PIPE)
        out, err = proc.communicate()
        for line in out.split("\n"):
            if line:
                self.go(main_repo, line.rstrip(), args.target)

    def go(self, main_repo, input, target):
        parts = input.split(" ")
        branch = parts[3]
        tip, err = main_repo.call(
            "git", "rev-parse", branch,
            stdout=subprocess.PIPE).communicate()
        mrg, err = main_repo.call(
            "git", "merge-base", branch, target,
            stdout=subprocess.PIPE).communicate()
        if tip == mrg:
            print input


class CleanSandbox(GithubCommand):
    """Cleans snoopys-sandbox repo after testing

Removes all branches from your fork of snoopys-sandbox
    """

    NAME = "clean-sandbox"

    def __init__(self, sub_parsers):
        super(CleanSandbox, self).__init__(sub_parsers)
        self.add_token_args()

        group = self.parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            '-f', '--force', action="store_true",
            help="Perform a clean of all non-master branches")
        group.add_argument(
            '-n', '--dry-run', action="store_true",
            help="Perform a dry-run without removing any branches")

        self.parser.add_argument("--skip", action="append", default=["master"])

    def __call__(self, args):
        super(CleanSandbox, self).__call__(args)
        self.login(args)

        gh_repo = self.gh.gh_repo("snoopys-sandbox")
        branches = gh_repo.repo.get_branches()
        for b in branches:
            if b.name in args.skip:
                if args.dry_run:
                    print "Would not delete", b.name
            elif args.dry_run:
                print "Would delete", b.name
            elif args.force:
                gh_repo.push(":%s" % b.name)
            else:
                raise Exception("Not possible!")


class Label(GithubCommand):
    """
    Query/add/remove labels from Github issues.
    """

    NAME = "label"

    def __init__(self, sub_parsers):
        super(Label, self).__init__(sub_parsers)
        self.add_token_args()

        self.parser.add_argument(
            'issue', nargs="*", type=int,
            help="The number of the issue to check")

        # Actions
        group = self.parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            '--add', action='append',
            help='List labels attached to the issue')
        group.add_argument(
            '--available', action='store_true',
            help='List all available labels for this repo')
        group.add_argument(
            '--list', action='store_true',
            help='List labels attached to the issue')

    def __call__(self, args):
        super(Label, self).__call__(args)
        self.login(args)

        main_repo = self.gh.git_repo(self.cwd)
        try:
            self.labels(args, main_repo)
        finally:
            main_repo.cleanup()

    def labels(self, args, main_repo):
        if args.add:
            self.add(args, main_repo)
        elif args.available:
            self.available(args, main_repo)
        elif args.list:
            self.list(args, main_repo)

    def get_issue(self, args, main_repo, issue):
        # Copied from Rebase command.
        # TODO: this could be refactored
        if args.issue and len(args.issue) > 1:
            print "# %s" % issue
        return main_repo.origin.get_issue(issue)

    def add(self, args, main_repo):
        for label in args.add:

            try:
                label = main_repo.origin.get_label(label)
            except github.GithubException, ge:
                if self.gh.exc_is_not_found(ge):
                    try:
                        main_repo.origin.create_label(label, "663399")
                        label = main_repo.origin.get_label(label)
                    except github.GithubException, ge:
                        if self.gh.exc_is_not_found(ge):
                            raise Stop(10, "Can't create label: %s" % label)
                        raise
                else:
                    raise

            for issue in args.issue:
                issue = self.get_issue(args, main_repo, issue)
                try:
                    issue.add_to_labels(label)
                except github.GithubException, ge:
                    if self.gh.exc_is_not_found(ge):
                        raise Stop(10, "Can't add label: %s" % label.name)
                    raise

    def available(self, args, main_repo):
        if args.issue:
            print >>sys.stderr, "# Ignoring issues: %s" % args.issue
        for label in main_repo.origin.get_labels():
            print label.name

    def list(self, args, main_repo):
        for issue in args.issue:
            issue = self.get_issue(args, main_repo, issue)
            labels = issue.get_labels()
            for label in labels:
                print label.name


class Merge(FilteredPullRequestsCommand):
    """
    Merge Pull Requests opened against a specific base branch.

    Automatically merge all pull requests with any of the given labels.
    It assumes that you have checked out the target branch locally and
    have updated any submodules. The SHA1s from the PRs will be merged
    into the current branch. AFTER the PRs are merged, any open PRs for
    each submodule with the same tags will also be merged into the
    CURRENT submodule sha1. A final commit will then update the submodules.
    """

    NAME = "merge"

    def __init__(self, sub_parsers):
        super(Merge, self).__init__(sub_parsers)
        self.parser.add_argument(
            '--comment', action='store_true',
            help='Add comment to conflicting PR')
        self.parser.add_argument(
            '--set-commit-status', action='store_true',
            help='Set success/failure status on latest commits in all PRs '
            'in the merge.')
        self.add_new_commit_args()

    def __call__(self, args):
        super(Merge, self).__call__(args)
        self.login(args)

        self.init_main_repo(args)

        try:
            updated = self.merge(args, self.main_repo)
        finally:
            if not args.info:
                self.log.debug("Cleaning remote branches created for merging")
                self.main_repo.rcleanup()

        if updated and args.push is not None:
            self.push(args, self.main_repo)

    def merge(self, args, main_repo):

        self._parse_filters(args)

        # Create commit message using command arguments
        commit_args = ["merge"]
        commit_args.append(args.base)
        commit_args.append("-D")
        commit_args.append(args.default)
        if args.include:
            for filt in args.include:
                commit_args.append("-I")
                commit_args.append(filt)
        if args.exclude:
            for filt in args.exclude:
                commit_args.append("-E")
                commit_args.append(filt)

        updated, merge_msg = main_repo.rmerge(
            self.filters, args.info,
            args.comment, commit_id=" ".join(commit_args),
            top_message=args.message,
            update_gitmodules=args.update_gitmodules,
            set_commit_status=args.set_commit_status)

        for line in merge_msg.split("\n"):
            self.log.info(line)
        return updated

    def _log_parse_filters(self, args, default_user):
        if args.info:
            action = "Finding"
        else:
            action = "Merging"
        self.log.info("%s PR based on %s opened by %s",
                      action, args.base, default_user)


class Rebase(GithubCommand):
    """Rebase Pull Requests opened against a specific base branch.

        The workflow currently is:

        1) Find the branch point for the original PR.
        2) Rebase all commits from the branch point to the tip.
        3) Create a branch named "rebase/develop/ORIG_NAME".
        4) If push is set, also push to GH, and switch branches.
        5) If pr is set, push to GH, open a PR, and switch branches.
        6) If keep is set, omit the deleting of the newbranch.

        If --remote is not set, 'origin' will be used.
    """

    NAME = "rebase"

    def __init__(self, sub_parsers):
        super(Rebase, self).__init__(sub_parsers)
        self.add_token_args()

        self.add_remote_arg()
        self.parser.add_argument(
            '--no-fetch', action='store_true',
            help="Do not fetch the origin remote")
        for name, help in (
                ('pr', 'Skip creating a PR.'),
                ('push', 'Skip pushing to Github'),
                ('delete', 'Skip deleting local branch')):

            self.parser.add_argument(
                '--no-%s' % name, action='store_false',
                dest=name, default=True, help=help)
        self.parser.add_argument(
            '--continue', action="store_true", dest="_continue",
            help="Continue from a failed rebase")

        self.parser.add_argument(
            'PR', type=int, help="The number of the pull request to rebase")
        self.parser.add_argument(
            'newbase', type=str,
            help="The branch of origin onto which the PR should be rebased")

    def __call__(self, args):
        super(Rebase, self).__call__(args)
        self.login(args)

        main_repo = self.gh.git_repo(self.cwd)
        try:
            if not args.no_fetch:
                main_repo.fetch(args.remote)
            self.rebase(args, main_repo)
        finally:
            main_repo.cleanup()

    def rebase(self, args, main_repo):

        # Local information
        [origin_name, origin_repo] = main_repo.get_remote_info(args.remote)
        # If we are pushing the branch somewhere, we likely will
        # be deleting the new one, and so should remember what
        # commit we are on now in order to go back to it.
        try:
            old_branch = main_repo.get_current_head()
        except:
            old_branch = main_repo.get_current_sha1()

        # Remote information
        try:
            pr = main_repo.origin.get_pull(args.PR)
            self.log.info("PR %g: %s opened by %s against %s",
                          args.PR, pr.title, pr.head.user.name, pr.base.ref)
        except github.GithubException:
            raise Stop(16, 'Cannot find pull request %s' % args.PR)

        pr_head = pr.head.sha
        self.log.info("Head: %s", pr_head[0:6])
        self.log.info("Merged: %s", pr.is_merged())

        # Fail-fast if bad object
        if not main_repo.has_local_object(pr_head):
            raise Stop(17, 'Commit %s does not exists in local Git '
                       'repository. Fetch this remote first: %s'
                       % (pr_head, pr.head.user.login))

        # Fail-fast if local branch exist with the target name
        new_branch = "rebased/%s/%s" % (args.newbase, pr.head.ref)
        if main_repo.has_local_branch(new_branch):
            raise Stop(18, 'Branch %s already exists in local Git repository'
                       % new_branch)

        remote_newbase = "%s/%s" % (args.remote, args.newbase)
        if not args._continue:
            branching_sha1 = main_repo.find_branching_point(
                pr_head, "%s/%s" % (args.remote, pr.base.ref))

            try:
                main_repo.rebase(remote_newbase, branching_sha1, pr_head)
            except:
                raise Stop(20, self.get_conflict_message(args))

        # Fail-fast if sha1 is the same as the new base

        if main_repo.get_current_sha1() == main_repo.get_sha1(remote_newbase):
            raise Stop(22, "No new commits between the rebased branch and %s"
                       % remote_newbase)
        main_repo.new_branch(new_branch)
        print >> sys.stderr, "# Created local branch %s" % new_branch

        if args.push or args.pr:
            try:
                user = self.gh.get_login()
                # Fail-fast if remote branch exist with the target name
                if main_repo.has_remote_branch(new_branch, remote=user):
                    raise Stop(19, 'Branch %s already exists in %s remote'
                               % (new_branch, args.remote))

                remote = "git@github.com:%s/%s.git" % (user, origin_repo)
                main_repo.push_branch(new_branch, remote=remote)
                print >> sys.stderr, "# Pushed %s to %s" % (new_branch, remote)

                if args.pr:
                    template_args = {
                        "id": pr.number, "base": args.newbase,
                        "title": pr.title, "body": pr.body}
                    title = "%(title)s (rebased onto %(base)s)" \
                        % template_args
                    body = """

This is the same as gh-%(id)s but rebased onto %(base)s.

----

%(body)s

                    """ % template_args

                    gh_repo = self.gh.gh_repo(origin_repo, origin_name)
                    rebased_pr = gh_repo.open_pr(
                        title, body,
                        base=args.newbase, head="%s:%s" % (user, new_branch))
                    print rebased_pr.html_url

                    # Add rebase comments
                    pr.create_issue_comment('--rebased-to #%s' %
                                            rebased_pr.number)
                    rebased_pr.create_issue_comment('--rebased-from #%s' %
                                                    pr.number)

            finally:
                main_repo.checkout_branch(old_branch)

            if args.delete:
                main_repo.delete_local_branch(new_branch, force=True)

    def get_conflict_message(self, args):
        msg = 'Rebasing failed\nYou are now in detached HEAD mode\n\n'
        msg += 'To keep on rebasing,\n'
        msg += '1) check the output of "git status" and fix the conflicts\n'
        msg += '2) re-add the conflicting files with "git add"\n'
        msg += '3) run "git rebase --continue"\n'
        msg += '4) repeat steps 1-3 until all conflicts are resolved\n'
        msg += '4) run "scc rebase --continue %s %s"\n\n' \
            % (args.PR, args.newbase)
        msg += 'To stop rebasing,\n'
        msg += '1) run "git rebase --abort"\n'
        msg += '2) checkout the desired branch, e.g "git checkout master"'
        return msg


class Token(GithubCommand):
    """Utility functions to manipulate local and remote Github tokens"""

    NAME = "token"

    def __init__(self, sub_parsers):
        super(Token, self).__init__(sub_parsers)
        # No token args

        token_parsers = self.parser.add_subparsers(title="Subcommands")
        self._configure(token_parsers)

    def _configure(self, sub_parsers):
        help = "Print all known Github tokens and users"
        list = sub_parsers.add_parser("list", help=help, description=help)
        list.set_defaults(func=self.list)

        help = """Create a new token and set the value of github token"""
        desc = help + ". See http://developer.github.com/v3/oauth/" \
            "#create-a-new-authorization for more information."
        create = sub_parsers.add_parser("create", help=help, description=desc)
        create.set_defaults(func=self.create)
        create.add_argument(
            '--no-set', action="store_true",
            help="Create the token but do not set it")
        create.add_argument(
            '--scope', '-s', type=str, action='append',
            default=DefaultList(["public_repo"]), choices=self.get_scopes(),
            help="Scopes to use for token creation. Default: ['public_repo']")

        help = "Set token to the specified value"
        set = sub_parsers.add_parser("set", help=help, description=help)
        set.add_argument('value', type=str, help="Value of the token to set")
        set.set_defaults(func=self.set)

        help = "Get the github token"
        get = sub_parsers.add_parser("get", help=help, description=help)
        get.set_defaults(func=self.get)

        for x in (create, set, get):
            self.add_config_file_arguments(x)

    def get_scopes(self):
        """List available scopes for authorization creation"""

        return ['user', 'user:email', 'user:follow', 'public_repo', 'repo',
                'repo:status', 'delete_repo', 'notifications', 'gist']

    def add_config_file_arguments(self, parser):
        parser.add_argument(
            "--local", action="store_true",
            help="Access token only in local repository")
        parser.add_argument(
            "--user", action="store_true",
            help="Access token only in user configuration")

    def list(self, args):
        """List existing github tokens and users"""

        super(Token, self).__call__(args)
        for key in ("github.token", "github.user"):
            for user, local, msg in \
                    ((False, True, "local"), (True, False, "user")):

                rv = git_config(key, user=user, local=local)
                if rv is not None:
                    print "[%s] %s=%s" % (msg, key, rv)

    def create(self, args):
        """Create a new github token"""

        super(Token, self).__call__(args)
        user = git_config("github.user")
        if not user:
            raise Exception("No github.user configured")
        gh = get_github(user)
        user = gh.github.get_user()
        auth = user.create_authorization(args.scope, "scc token")
        print "Created authentification token %s" % auth.token
        if not args.no_set:
            git_config("github.token", user=args.user,
                       local=args.local, value=auth.token)

    def get(self, args):
        """Get the value of the github token"""

        super(Token, self).__call__(args)
        token = git_config("github.token",
                           user=args.user, local=args.local)
        if token:
            print token

    def set(self, args):
        """Set the value of the github token"""

        super(Token, self).__call__(args)
        git_config("github.token", user=args.user,
                   local=args.local, value=args.value)
        return


class TravisMerge(GitRepoCommand):
    """
    Update submodules and merge Pull Requests in Travis CI jobs.

    Use the Travis environment variable to read the pull request number. Read
    the base branch using the Github API.
    """

    NAME = "travis-merge"

    def __init__(self, sub_parsers):
        super(TravisMerge, self).__init__(sub_parsers)
        self.parser.add_argument(
            '--info', action='store_true',
            help='Display merge candidates but do not merge them')

    def __call__(self, args):
        super(TravisMerge, self).__call__(args)
        args.no_ask = True  # Do not ask for login
        self.login(args)

        # Read pull request number from environment variable
        pr_key = 'TRAVIS_PULL_REQUEST'
        if pr_key in os.environ:
            pr_number = os.environ.get(pr_key)
            if pr_number == 'false':
                raise Stop(0, "Travis job is not a pull request")
        else:
            raise Stop(51, "No %s found. Re-run this command within a Travis"
                       " environment" % pr_key)

        args.reset = False
        self.init_main_repo(args)

        pr = PullRequest(self.main_repo.origin.get_pull(int(pr_number)))

        # Parse comments for companion PRs inclusion in the Travis build
        self._parse_dependencies(pr.get_base(),
                                 pr.parse_comments('depends-on'))

        try:
            updated, merge_msg = self.main_repo.rmerge(self.filters,
                                                       args.info)
            for line in merge_msg.split("\n"):
                self.log.info(line)
        finally:
            if not args.info:
                self.log.debug("Cleaning remote branches created for merging")
                self.main_repo.rcleanup()

    def _parse_dependencies(self, base, comments):
        # Create default merge filters using the PR base ref
        self.filters = {}
        self.filters["base"] = base
        self.filters["default"] = "none"
        self.filters["include"] = {"label": None, "user": None, "pr": None}
        self.filters["exclude"] = {"label": None, "user": None, "pr": None}

        for comment in comments:
            dep = comment.strip()
            # Look for #value pattern
            pattern = "#"
            if dep.find(pattern) != -1:
                pr = dep.replace(pattern, '', 1)
                if self.filters["include"]["pr"]:
                    self.filters["include"]["pr"].append(pr)
                else:
                    self.filters["include"]["pr"] = [pr]


class UnrebasedPRs(GitRepoCommand):
    """Check that PRs in one branch have been merged to another.

This makes use of git notes to detect links between PRs on two
different branches. These have likely be migrated via the rebase
command.

    """

    NAME = "unrebased-prs"

    def __init__(self, sub_parsers):
        super(UnrebasedPRs, self).__init__(sub_parsers)
        group = self.parser.add_mutually_exclusive_group()
        group.add_argument(
            '--parse', action='store_true',
            help="Parse generated files into git commands")
        group.add_argument(
            '--write', action='store_true',
            help="Write PRs to files.")
        group.add_argument(
            '--no-check', action='store_true',
            help="Do not check mismatching rebased PR comments.")

        self.parser.add_argument('a', help="First branch to compare")
        self.parser.add_argument('b', help="Second branch to compare")

    def fname(self, branch):
        return "%s_prs.txt" % branch

    def __call__(self, args):
        super(UnrebasedPRs, self).__call__(args)
        self.login(args)

        self.init_main_repo(args)

        try:
            self.notes(args)
        finally:
            self.main_repo.cleanup()

    def notes(self, args):
        if args.parse:
            self.parse(args.a, args.b)
        else:
            d1 = self.list_prs(args.a, args.b, remote=args.remote,
                               write=args.write)
            d2 = self.list_prs(args.b, args.a, remote=args.remote,
                               write=args.write)

            if not args.no_check:
                m = self.check_links(d1, d2, args.a, args.b)
                if not m:
                    return

                print "*"*100
                print "Mismatching rebased PR comments"
                print "*"*100

                for key in m.keys():
                    comments = ", ".join(['--rebased'+x for x in m[key]])
                    print "  # PR %s: expected '%s' comment(s)" %  \
                        (key, comments)

    def parse(self, branch1, branch2):
        aname = self.fname(branch1)
        bname = self.fname(branch2)
        if not os.path.exists(aname) or not os.path.exists(bname):
            print 'Use --write to create files first'

        alines = open(aname, "r").read().strip().split("\n")
        blines = open(bname, "r").read().strip().split("\n")

        if len(alines) != len(blines):
            print 'Size of files does not match! (%s <> %s)' \
                % (len(alines), len(blines))
            print 'Edit files so that lines match'

        fmt_gh = "git notes --ref=see_also/%s append" \
            " -m 'See gh-%s on %s (%s)' %s"
        fmt_na = "git notes --ref=see_also/%s append -m '%s' %s"
        for i, a in enumerate(alines):
            b = blines[i]
            try:
                aid, apr, arest = self.parse_pr(a)
            except Exception, e:
                try:
                    aid, arest = self.parse_commit(a)
                except:
                    aid = None
                    apr = None
                    arest = e.line

            try:
                bid, bpr, brest = self.parse_pr(b)
            except Exception, e:
                try:
                    bid, brest = self.parse_commit(b)
                except:
                    bid = None
                    bpr = None
                    brest = e.line

            if aid and bid:
                print fmt_gh % (branch2, bpr, branch2, bid, aid)
                print fmt_gh % (branch1, apr, branch1, aid, bid)
            elif aid:
                print fmt_na % (branch2, brest, aid)
            elif bid:
                print fmt_na % (branch1, arest, bid)
            else:
                raise Exception("No IDs found for line %s!" % i)

    def list_prs(self, current, seealso, remote="origin", write=False):
        """
        Method for listing PRs while filtering out those which
        have a seealso note
        """
        git_notes_ref = "refs/notes/see_also/" + seealso
        merge_base = self.main_repo.merge_base(
            "%s/%s" % (remote, current),
            "%s/%s" % (remote, seealso))
        merge_range = "%s...%s/%s" % (merge_base, remote, current)
        middle_marker = str(uuid.uuid4()).replace("-", "")
        end_marker = str(uuid.uuid4()).replace("-", "")

        popen = self.main_repo.call_no_wait(
            "git", "log",
            "--pretty=%%h %%s %%ar %s %%N %s" % (middle_marker, end_marker),
            "--notes=%s" % git_notes_ref,
            "--first-parent", merge_range,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if write:
            fname = self.fname(current)
            if os.path.exists(fname):
                raise Stop("File already exists: %s" % fname)
            f = open(fname, "w")
        else:
            print "*"*100
            print "PRs on %s without note/comment for %s" % (current, seealso)
            print "*"*100

        # List PRs without seealso notes
        pr_list = []
        out, err = popen.communicate()
        for line in out.split(end_marker):
            line = line.strip()
            if not line:
                continue
            try:
                line, rest = line.split(middle_marker)
            except:
                raise Exception("can't split on ##: " + line)
            if "See gh-" in rest or "n/a" in rest:
                continue

            try:
                sha1, num, rest = self.parse_pr(line)
                pr_list.append(num)
            except:
                self.log.info("Unknown merge: %s", line)
                continue

        # Look into PR body/comment for rebase notes and fill match dictionary
        pr_dict = dict.fromkeys(pr_list)
        for pr_number in pr_list:
            pr = PullRequest(self.main_repo.origin.get_pull(pr_number))

            rebased_notes = pr.parse(['rebased', 'no-rebase'])
            if rebased_notes:
                pr_dict[pr_number] = rebased_notes
                continue

            if write:
                print >>f, pr
            else:
                print pr
        return pr_dict

    def check_links(self, d1, d2, branch1, branch2):
        """Return a dictionary of PRs with missing comments"""

        m1 = self.check_directed_links(d2, d1)
        m2 = self.check_directed_links(d1, d2)

        def visit_pr(pr_number, branch):
            pr = PullRequest(self.main_repo.origin.get_pull(pr_number))
            if (pr.pull.state == 'open' or pr.pull.is_merged()) and \
                    pr.get_base() == branch:
                return pr.parse(['rebased', 'no-rebase'])
            else:
                return None

        # Ensure all nodes (PRs) are visited - handling chained links
        while not all(x in d1.keys() for x in m1.keys()) or \
                not all(x in d2.keys() for x in m2.keys()):

            for pr_number in [key for key in m1.keys()
                              if not key in d1.keys()]:
                d1[pr_number] = visit_pr(pr_number, branch1)

            for pr_number in [key for key in m2.keys()
                              if not key in d2.keys()]:
                d2[pr_number] = visit_pr(pr_number, branch2)

            m1 = self.check_directed_links(d2, d1)
            m2 = self.check_directed_links(d1, d2)

        m1.update(m2)
        return m1

    @staticmethod
    def check_directed_links(source_dict, target_dict):
        """Find mismatching comments in rebased PRs"""

        mismatch_dict = {}
        for source_key in source_dict.keys():
            if source_dict[source_key] is None:
                continue

            to_pattern = r"-to #(\d+)"
            from_pattern = r"-from #(\d+)"
            for source_value in source_dict[source_key]:
                match = re.match(to_pattern, source_value)
                if match:
                    target_value = '-from #%s' % source_key
                else:
                    match = re.match(from_pattern, source_value)
                    if match:
                        target_value = '-to #%s' % source_key
                    else:
                        continue

                target_key = int(match.group(1))
                if target_key not in target_dict or \
                   target_dict[target_key] is None or \
                   not any(x.startswith(target_value) for x
                           in target_dict[target_key]):

                    if target_key in mismatch_dict:
                        mismatch_dict[target_key].append(target_value)
                    else:
                        mismatch_dict[target_key] = [target_value]
        return mismatch_dict


class UpdateSubmodules(GitRepoCommand):
    """
    Similar to the 'merge' command, but only updates submodule pointers.
    """

    NAME = "update-submodules"

    def __init__(self, sub_parsers):
        super(UpdateSubmodules, self).__init__(sub_parsers)

        self.parser.add_argument(
            '--no-fetch', action='store_true',
            help="Fetch the latest target branch for all repos")
        self.parser.add_argument(
            '--no-pr', action='store_false',
            dest='pr', default=True, help='Skip creating a PR.')
        self.add_new_commit_args()

    def __call__(self, args):
        super(UpdateSubmodules, self).__call__(args)
        self.login(args)

        self.init_main_repo(args)

        try:
            if args.message is None:
                args.message = "Update %s submodules" % args.base
            self.log.info(args.message)
            updated, merge_msg = self.submodules(args, self.main_repo)

            if updated and args.push is not None:
                self.push(args, self.main_repo)

                if args.pr:

                    pr = self.get_open_pr(args)
                    body = merge_msg
                    if IS_JENKINS_JOB:
                        body += "\n\nGenerated by build [%s#%s](%s)." % \
                            (JOB_NAME, BUILD_NUMBER, BUILD_URL)
                    body += "\n\n----\n--no-rebase"

                    if pr is None:
                        title = args.message
                        user = self.gh.get_login()
                        pr = self.main_repo.origin.open_pr(
                            title, body,
                            base=args.base,
                            head="%s:%s" % (user, args.push))
                        self.log.info("New PR created: %s", pr.html_url)
                    else:
                        pr.edit_body(body)
                        self.log.info("PR %s updated", pr.get_number())
        finally:
            self.main_repo.rcleanup()

    def submodules(self, args, main_repo):
        for submodule in main_repo.submodules:
            submodule.cd(submodule.path)
            if not args.no_fetch:
                submodule.fetch(args.remote)
            #submodule.checkout_branch("%s/%s" % (args.remote, args.base))

        # Create commit message using command arguments
        self.filters = {}
        self.filters["base"] = args.base
        self.filters["default"] = "none"
        self.filters["include"] = {"label": None, "user": None, "pr": None}
        self.filters["exclude"] = {"label": None, "user": None, "pr": None}

        updated, merge_msg = main_repo.rmerge(
            self.filters,
            top_message=args.message,
            update_gitmodules=args.update_gitmodules)
        for line in merge_msg.split("\n"):
            self.log.info(line)
        return updated, merge_msg


class SetCommitStatus(FilteredPullRequestsCommand):
    """
    Set commit status on all pull requests with any of the given labels.
    It assumes that you have checked out the target branch locally and
    have updated any submodules.
    """

    NAME = "set-commit-status"

    def __init__(self, sub_parsers):
        super(SetCommitStatus, self).__init__(sub_parsers)

        self.parser.add_argument(
            '--status', '-s', type=str, required=True,
            choices=["success", "failure", "error", "pending"],
            help='Commit status.')
        self.parser.add_argument(
            '--message', '-m', required=True,
            help='Message to use for the commit status.')
        self.parser.add_argument(
            '--url', '-u',
            help='URL to use for the commit status.')
        self.parser.add_argument('base', type=str)

    def __call__(self, args):
        super(SetCommitStatus, self).__call__(args)
        self.login(args)
        self.init_main_repo(args)
        self.setCommitStatus(args, self.main_repo)

    def setCommitStatus(self, args, main_repo):
        self._parse_filters(args)
        msg = main_repo.rset_commit_status(
            self.filters, args.status, args.message,
            args.url, info=args.info)
        for line in msg.split("\n"):
            self.log.info(line)


class TagRelease(GitRepoCommand):
    """
    Tag a release recursively across submodules.
    """

    NAME = "tag-release"

    def __init__(self, sub_parsers):
        super(TagRelease, self).__init__(sub_parsers)

        self.parser.add_argument(
            'version', type=str,
            help='Version number to use to construct the tag')
        self.parser.add_argument(
            '--message', '-m', type=str,
            help='Tag message')
        self.parser.add_argument(
            '--push', action='store_true',
            help='Push new tag to Github')

    def __call__(self, args):
        super(TagRelease, self).__call__(args)

        if not self.check_version_format(args):
            raise Stop(23, '%s is not a valid version number. '
                       'See http://semver.org for more information.'
                       % args.version)

        self.login(args)
        self.init_main_repo(args)
        if args.message is None:
            args.message = 'Tag version %s' % args.version
        msg = self.main_repo.rtag(args.version, message=args.message)

        for line in msg.split("\n"):
            self.log.info(line)

        if args.push:
            user = self.gh.get_login()
            remote = "git@github.com:%s/" % (user) + "%s.git"
            self.main_repo.rpush('--tags', remote, force=True)

    def check_version_format(self, args):
        """Check format of version number"""

        import re
        pattern = '^[0-9]+[\.][0-9]+[\.][0-9]+(\-.+)*$'
        return re.match(pattern, args.version) is not None
