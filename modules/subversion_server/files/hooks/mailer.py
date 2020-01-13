#!/usr/bin/python
#
# mailer.py: send email describing a commit
#
# $HeadURL: http://svn.collab.net/repos/svn/branches/1.1.x/hook-scripts/mailer/mailer.py $
# $LastChangedDate: 2004-08-09 14:32:31 -0700 (Mon, 09 Aug 2004) $
# $LastChangedBy: breser $
# $LastChangedRevision: 10538 $
#
# USAGE: mailer.py commit     REPOS-DIR REVISION [CONFIG-FILE]
#        mailer.py propchange REPOS-DIR REVISION AUTHOR PROPNAME [CONFIG-FILE]
#
#   Using CONFIG-FILE, deliver an email describing the changes between
#   REV and REV-1 for the repository REPOS.
#

import os
import sys
import string
import ConfigParser
import time
import popen2
import cStringIO
import smtplib
import re
import urllib

import svn.fs
import svn.delta
import svn.repos
import svn.core

SEPARATOR = '=' * 78


def main(pool, cmd, config_fname, repos_dir, rev, author, propname):
  repos = Repository(repos_dir, rev, pool)

  if cmd == 'commit':
    cfg = Config(config_fname, repos, { 'author' : author or repos.author })
    messenger = Commit(pool, cfg, repos)
  elif cmd == 'propchange':
    # Override the repos revision author with the author of the propchange
    repos.author = author
    cfg = Config(config_fname, repos, { 'author' : author })
    messenger = PropChange(pool, cfg, repos, author, propname)
  else:
    raise UnknownSubcommand(cmd)

  messenger.generate()


class MailedOutput:
  def __init__(self, cfg, repos, prefix_param):
    self.cfg = cfg
    self.repos = repos
    self.prefix_param = prefix_param

  def start(self, group, params):
    # whitespace-separated list of addresses; split into a clean list:
    self.to_addrs = \
        filter(None, string.split(self.cfg.get('to_addr', group, params)))
    self.from_addr = self.cfg.get('from_addr', group, params) \
                     or self.repos.author or 'no_author'
    self.reply_to = self.cfg.get('reply_to', group, params)

  def mail_headers(self, group, params):
    prefix = self.cfg.get(self.prefix_param, group, params)
    if prefix:
      subject = prefix + ' ' + self.subject
    else:
      subject = self.subject
    hdrs = 'From: %s\n'    \
           'To: %s\n'      \
           'Subject: %s\n' \
           'MIME-Version: 1.0\n' \
           'Content-Type: text/plain; charset=UTF-8\n' \
           % (self.from_addr, string.join(self.to_addrs, ', '), subject)
    if self.reply_to:
      hdrs = '%sReply-To: %s\n' % (hdrs, self.reply_to)
    return hdrs + '\n'


class SMTPOutput(MailedOutput):
  "Deliver a mail message to an MTA using SMTP."

  def start(self, group, params, **args):
    MailedOutput.start(self, group, params, **args)

    self.buffer = cStringIO.StringIO()
    self.write = self.buffer.write

    self.write(self.mail_headers(group, params))

  def run(self, cmd):
    # we're holding everything in memory, so we may as well read the
    # entire diff into memory and stash that into the buffer
    pipe_ob = popen2.Popen3(cmd)
    self.write(pipe_ob.fromchild.read())

    # wait on the child so we don't end up with a billion zombies
    pipe_ob.wait()

  def finish(self):
    if self.cfg.is_set('general.smtp_debug'):
      print self.buffer.getvalue()
      return
    server = smtplib.SMTP(self.cfg.general.smtp_hostname)
    if self.cfg.is_set('general.smtp_username'):
      server.login(self.cfg.general.smtp_username,
                   self.cfg.general.smtp_password)
    server.sendmail(self.from_addr, self.to_addrs, self.buffer.getvalue())
    server.quit()


class StandardOutput:
  "Print the commit message to stdout."

  def __init__(self, cfg, repos, prefix_param):
    self.cfg = cfg
    self.repos = repos

    self.write = sys.stdout.write

  def start(self, group, params, **args):
    pass

  def finish(self):
    pass

  def run(self, cmd):
    # flush our output to keep the parent/child output in sync
    sys.stdout.flush()
    sys.stderr.flush()

    # we can simply fork and exec the diff, letting it generate all the
    # output to our stdout (and stderr, if necessary).
    pid = os.fork()
    if pid:
      # in the parent. we simply want to wait for the child to finish.
      ### should we deal with the return values?
      os.waitpid(pid, 0)
    else:
      # in the child. run the diff command.
      try:
        os.execvp(cmd[0], cmd)
      finally:
        os._exit(1)


class PipeOutput(MailedOutput):
  "Deliver a mail message to an MDA via a pipe."

  def __init__(self, cfg, repos, prefix_param):
    MailedOutput.__init__(self, cfg, repos, prefix_param)

    # figure out the command for delivery
    self.cmd = string.split(cfg.general.mail_command)

    # we want a descriptor to /dev/null for hooking up to the diffs' stdin
    self.null = os.open('/dev/null', os.O_RDONLY)

  def start(self, group, params, **args):
    MailedOutput.start(self, group, params, **args)

    ### gotta fix this. this is pretty specific to sendmail and qmail's
    ### mailwrapper program. should be able to use option param substitution
    cmd = self.cmd + [ '-f', self.from_addr ] + self.to_addrs

    # construct the pipe for talking to the mailer
    self.pipe = popen2.Popen3(cmd)
    self.write = self.pipe.tochild.write

    # we don't need the read-from-mailer descriptor, so close it
    self.pipe.fromchild.close()

    # start writing out the mail message
    self.write(self.mail_headers(group, params))

  def run(self, cmd):
    # flush the buffers that write to the mailer. we're about to fork, and
    # we don't want data sitting in both copies of the buffer. we also
    # want to ensure the parts are delivered to the mailer in the right order.
    self.pipe.tochild.flush()

    pid = os.fork()
    if pid:
      # in the parent

      # wait for the diff to finish
      ### do anything with the return value?
      os.waitpid(pid, 0)

      return

    # in the child

    # duplicate the write-to-mailer descriptor to our stdout and stderr
    os.dup2(self.pipe.tochild.fileno(), 1)
    os.dup2(self.pipe.tochild.fileno(), 2)

    # hook up stdin to /dev/null
    os.dup2(self.null, 0)

    ### do we need to bother closing self.null and self.pipe.tochild ?

    # run the diff command, now that we've hooked everything up
    try:
      os.execvp(cmd[0], cmd)
    finally:
      os._exit(1)

  def finish(self):
    # signal that we're done sending content
    self.pipe.tochild.close()

    # wait to avoid zombies
    self.pipe.wait()


class Messenger:
  def __init__(self, pool, cfg, repos, prefix_param):
    self.pool = pool
    self.cfg = cfg
    self.repos = repos
    self.determine_output(cfg, repos, prefix_param)

  def determine_output(self, cfg, repos, prefix_param):
    if cfg.is_set('general.mail_command'):
      cls = PipeOutput
    elif cfg.is_set('general.smtp_hostname'):
      cls = SMTPOutput
    else:
      cls = StandardOutput

    self.output = cls(cfg, repos, prefix_param)


class Commit(Messenger):
  def __init__(self, pool, cfg, repos):
    Messenger.__init__(self, pool, cfg, repos, 'commit_subject_prefix')

    # get all the changes and sort by path
    editor = svn.repos.RevisionChangeCollector(repos.fs_ptr, repos.rev,
                                               self.pool)
    e_ptr, e_baton = svn.delta.make_editor(editor, self.pool)
    svn.repos.svn_repos_replay(repos.root_this, e_ptr, e_baton, self.pool)

    self.changelist = editor.changes.items()
    self.changelist.sort()

    ### hunh. this code isn't actually needed for StandardOutput. refactor?
    # collect the set of groups and the unique sets of params for the options
    self.groups = { }
    paths = []
    for path, change in self.changelist:
      paths.append('/' + path)
      for (group, params) in self.cfg.which_groups(path):
        # turn the params into a hashable object and stash it away
        param_list = params.items()
        param_list.sort()
        self.groups[group, tuple(param_list)] = params

    paths_str = string.join(paths, ' ')
    if len(paths_str) < 256:
      self.output.subject = 'r%d - %s' % (repos.rev, paths_str)
      return 
      
    # figure out the changed directories
    dirs = { }
    for path, change in self.changelist:
      if change.item_kind == svn.core.svn_node_dir:
        dirs[path] = None
      else:
        idx = string.rfind(path, '/')
        if idx == -1:
          dirs[''] = None
        else:
          dirs[path[:idx]] = None

    dirlist = dirs.keys()

    # figure out the common portion of all the dirs. note that there is
    # no "common" if only a single dir was changed, or the root was changed.
    if len(dirs) == 1 or dirs.has_key(''):
      commondir = ''
    else:
      common = string.split(dirlist.pop(), '/')
      for d in dirlist:
        parts = string.split(d, '/')
        for i in range(len(common)):
          if i == len(parts) or common[i] != parts[i]:
            del common[i:]
            break
      commondir = string.join(common, '/')
      if commondir:
        # strip the common portion from each directory
        l = len(commondir) + 1
        dirlist = [ ]
        for d in dirs.keys():
          if d == commondir:
            dirlist.append('.')
          else:
            dirlist.append(d[l:])
      else:
        # nothing in common, so reset the list of directories
        dirlist = dirs.keys()

    # compose the basic subject line. later, we can prefix it.
    dirlist.sort()
    dirlist = string.join(dirlist)
    if commondir:
      self.output.subject = 'r%d - in %s: %s' % (repos.rev, commondir, dirlist)
    else:
      self.output.subject = 'r%d - %s' % (repos.rev, dirlist)

  def generate(self):
    "Generate email for the various groups and option-params."

    ### the groups need to be further compressed. if the headers and
    ### body are the same across groups, then we can have multiple To:
    ### addresses. SMTPOutput holds the entire message body in memory,
    ### so if the body doesn't change, then it can be sent N times
    ### rather than rebuilding it each time.

    subpool = svn.core.svn_pool_create(self.pool)

    for (group, param_tuple), params in self.groups.items():
      self.output.start(group, params)

      # generate the content for this group and set of params
      generate_content(self.output, self.cfg, self.repos, self.changelist,
                       group, params, subpool)

      self.output.finish()
      svn.core.svn_pool_clear(subpool)

    svn.core.svn_pool_destroy(subpool)


class PropChange(Messenger):
  def __init__(self, pool, cfg, repos, author, propname):
    Messenger.__init__(self, pool, cfg, repos, 'propchange_subject_prefix')
    self.author = author
    self.propname = propname

    ### hunh. this code isn't actually needed for StandardOutput. refactor?
    # collect the set of groups and the unique sets of params for the options
    self.groups = { }
    for (group, params) in self.cfg.which_groups(''):
      # turn the params into a hashable object and stash it away
      param_list = params.items()
      param_list.sort()
      self.groups[group, tuple(param_list)] = params

    self.output.subject = 'r%d - %s' % (repos.rev, propname)

  def generate(self):
    for (group, param_tuple), params in self.groups.items():
      self.output.start(group, params)
      self.output.write('Author: %s\nRevision: %s\nProperty Name: %s\n\n'
                        % (self.author, self.repos.rev, self.propname))
      propvalue = self.repos.get_rev_prop(self.propname)
      self.output.write('New Property Value:\n')
      self.output.write(propvalue)
      self.output.finish()


def generate_content(output, cfg, repos, changelist, group, params, pool):

  svndate = repos.get_rev_prop(svn.core.SVN_PROP_REVISION_DATE)
  ### pick a different date format?
  date = time.ctime(svn.core.secs_from_timestr(svndate, pool))

  output.write('Author: %s\nDate: %s\nNew Revision: %s\n\n'
               % (repos.author, date, repos.rev))

  viewcvs_base_url = cfg.get('viewcvs_base_url', group, params)
  if viewcvs_base_url and len(viewcvs_base_url):
    output.write('URL: %s?view=rev&rev=%d\n'
                 % (viewcvs_base_url, repos.rev))

  output.write('Log:\n%s\n'
               % (repos.get_rev_prop(svn.core.SVN_PROP_REVISION_LOG) or ''))

  # print summary sections
  generate_list(output, 'Added', changelist, _select_adds)
  generate_list(output, 'Removed', changelist, _select_deletes)
  generate_list(output, 'Modified', changelist, _select_modifies)

  # these are sorted by path already
  for path, change in changelist:
    generate_diff(output, cfg, repos, date, change, group, params, pool)


def _select_adds(change):
  return change.added
def _select_deletes(change):
  return change.path is None
def _select_modifies(change):
  return not change.added and change.path is not None


def generate_list(output, header, changelist, selection):
  items = [ ]
  for path, change in changelist:
    if selection(change):
      items.append((path, change))
  if items:
    output.write('%s:\n' % header)
    for fname, change in items:
      if change.item_kind == svn.core.svn_node_dir:
        is_dir = '/'
      else:
        is_dir = ''
      if change.prop_changes:
        if change.text_changed:
          props = '   (contents, props changed)'
        else:
          props = '   (props changed)'
      else:
        props = ''
      output.write('   %s%s%s\n' % (fname, is_dir, props))
      if change.added and change.base_path:
        if is_dir:
          text = ''
        elif change.text_changed:
          text = ', changed'
        else:
          text = ' unchanged'
        output.write('      - copied%s from r%d, %s%s\n'
                     % (text, change.base_rev, change.base_path[1:], is_dir))


def generate_diff(output, cfg, repos, date, change, group, params, pool):
  if change.item_kind == svn.core.svn_node_dir:
    # all changes were printed in the summary. nothing to do.
    return

  gen_diffs = cfg.get('generate_diffs', group, params)
  viewcvs_base_url = cfg.get('viewcvs_base_url', group, params)

  ### Do a little dance for deprecated options.  Note that even if you
  ### don't have an option anywhere in your configuration file, it
  ### still gets returned as non-None.
  if len(gen_diffs):
    diff_add = False
    diff_copy = False
    diff_delete = False
    diff_modify = False
    list = string.split(gen_diffs, " ")
    for item in list:
      if item == 'add':
        diff_add = True
      if item == 'copy':
        diff_copy = True
      if item == 'delete':
        diff_delete = True
      if item == 'modify':
        diff_modify = True
  else:
    diff_add = True
    diff_copy = True
    diff_delete = True
    diff_modify = True
    ### These options are deprecated
    suppress = cfg.get('suppress_deletes', group, params)
    if suppress == 'yes':
      diff_delete = False
    suppress = cfg.get('suppress_adds', group, params)
    if suppress == 'yes':
      diff_add = False

  # Figure out if we're supposed to show ViewCVS URLs
  if len(viewcvs_base_url):
    show_urls = True
  else:
    show_urls = False
    
  diff = None
  diff_url = None
  header = None
  
  if not change.path:
    ### params is a bit silly here

    header = 'Deleted: %s' % change.base_path
    if show_urls:
      diff_url = '%s/%s?view=auto&rev=%d' \
                 % (viewcvs_base_url,
                    urllib.quote(change.base_path[1:]), change.base_rev)
    if diff_delete:
      diff = svn.fs.FileDiff(repos.get_root(change.base_rev),
                             change.base_path, None, None, pool)
      label1 = '%s\t%s' % (change.base_path, date)
      label2 = '(empty file)'
      singular = True

  elif change.added:
    if change.base_path and (change.base_rev != -1): # this file was copied.
      # note that we strip the leading slash from the base (copyfrom) path
      header = 'Copied: %s (from r%d, %s)' \
               % (change.path, change.base_rev, change.base_path[1:])
      if show_urls:
        diff_url = '%s/%s?view=diff&rev=%d&p1=%s&r1=%d&p2=%s&r2=%d' \
                   % (viewcvs_base_url,
                      urllib.quote(change.path), repos.rev,
                      urllib.quote(change.base_path[1:]), change.base_rev,
                      urllib.quote(change.path), repos.rev)
      if change.text_changed and diff_copy:
        diff = svn.fs.FileDiff(repos.get_root(change.base_rev),
                               change.base_path[1:],
                               repos.root_this, change.path,
                               pool)
        label1 = change.base_path[1:] + '\t(original)'
        label2 = '%s\t%s' % (change.path, date)
        singular = False
    else:
      header = 'Added: %s' % change.path
      if show_urls:
        diff_url = '%s/%s?view=auto&rev=%d' \
                   % (viewcvs_base_url,
                      urllib.quote(change.path), repos.rev)
      if diff_add:
        diff = svn.fs.FileDiff(None, None, repos.root_this, change.path, pool)
        label1 = '(empty file)'
        label2 = '%s\t%s' % (change.path, date)
        singular = True
  elif not change.text_changed:
    # don't bother to show an empty diff. prolly just a prop change.
    return
  else:
    header = 'Modified: %s' % change.path
    if show_urls:
      diff_url = '%s/%s?view=diff&rev=%d&p1=%s&r1=%d&p2=%s&r2=%d' \
                 % (viewcvs_base_url,
                    urllib.quote(change.path), repos.rev,
                    urllib.quote(change.base_path[1:]), change.base_rev,
                    urllib.quote(change.path), repos.rev)
    if diff_modify:
      diff = svn.fs.FileDiff(repos.get_root(change.base_rev),
                             change.base_path[1:],
                             repos.root_this, change.path,
                             pool)
      label1 = change.base_path[1:] + '\t(original)'
      label2 = '%s\t%s' % (change.path, date)
      singular = False

  if not (show_urls or diff):
    return
  
  output.write('\n' + header + '\n')
  if diff_url:
    output.write('Url: ' + diff_url + '\n')
  output.write(SEPARATOR + '\n')
  if not diff:
    return
  
  if diff.either_binary():
    if singular:
      output.write('Binary file. No diff available.\n')
    else:
      output.write('Binary files. No diff available.\n')
    return

  ### do something with change.prop_changes

  src_fname, dst_fname = diff.get_files()

  output.run(cfg.get_diff_cmd({
    'label_from' : label1,
    'label_to' : label2,
    'from' : src_fname,
    'to' : dst_fname,
    }))


class Repository:
  "Hold roots and other information about the repository."

  def __init__(self, repos_dir, rev, pool):
    self.repos_dir = repos_dir
    self.rev = rev
    self.pool = pool

    self.repos_ptr = svn.repos.svn_repos_open(repos_dir, pool)
    self.fs_ptr = svn.repos.svn_repos_fs(self.repos_ptr)

    self.roots = { }

    self.root_this = self.get_root(rev)

    self.author = self.get_rev_prop(svn.core.SVN_PROP_REVISION_AUTHOR)

  def get_rev_prop(self, propname):
    return svn.fs.revision_prop(self.fs_ptr, self.rev, propname, self.pool)

  def get_root(self, rev):
    try:
      return self.roots[rev]
    except KeyError:
      pass
    root = self.roots[rev] = svn.fs.revision_root(self.fs_ptr, rev, self.pool)
    return root


class Config:

  # The predefined configuration sections. These are omitted from the
  # set of groups.
  _predefined = ('general', 'defaults')

  def __init__(self, fname, repos, global_params):
    cp = ConfigParser.ConfigParser()
    cp.read(fname)

    # record the (non-default) groups that we find
    self._groups = [ ]

    for section in cp.sections():
      if not hasattr(self, section):
        section_ob = _sub_section()
        setattr(self, section, section_ob)
        if section not in self._predefined:
          self._groups.append((section, section_ob))
      else:
        section_ob = getattr(self, section)
      for option in cp.options(section):
        # get the raw value -- we use the same format for *our* interpolation
        value = cp.get(section, option, raw=1)
        setattr(section_ob, option, value)

    ### do some better splitting to enable quoting of spaces
    self._diff_cmd = string.split(self.general.diff)

    # these params are always available, although they may be overridden
    self._global_params = global_params.copy()

    self._prep_groups(repos)

  def get_diff_cmd(self, args):
    cmd = [ ]
    for part in self._diff_cmd:
      cmd.append(part % args)
    return cmd

  def is_set(self, option):
    """Return None if the option is not set; otherwise, its value is returned.

    The option is specified as a dotted symbol, such as 'general.mail_command'
    """
    parts = string.split(option, '.')
    ob = self
    for part in string.split(option, '.'):
      if not hasattr(ob, part):
        return None
      ob = getattr(ob, part)
    return ob

  def get(self, option, group, params):
    if group:
      sub = getattr(self, group)
      if hasattr(sub, option):
        return getattr(sub, option) % params
    return getattr(self.defaults, option, '') % params

  def _prep_groups(self, repos):
    self._group_re = [ ]
    self._last_group_re = [ ]

    repos_dir = os.path.abspath(repos.repos_dir)

    # compute the default repository-based parameters. start with some
    # basic parameters, then bring in the regex-based params.
    default_params = self._global_params.copy()

    try:
      match = re.match(self.defaults.for_repos, repos_dir)
      if match:
        default_params.update(match.groupdict())
    except AttributeError:
      # there is no self.defaults.for_repos
      pass

    # select the groups that apply to this repository
    for group, sub in self._groups:
      params = default_params
      if hasattr(sub, 'for_repos'):
        match = re.match(sub.for_repos, repos_dir)
        if not match:
          continue
        params = self._global_params.copy()
        params.update(match.groupdict())

      # if a matching rule hasn't been given, then use the empty string
      # as it will match all paths
      for_paths = getattr(sub, 'for_paths', '')
      exclude_paths = getattr(sub, 'exclude_paths', None)
      suppress_if_match = getattr(sub, 'suppress_if_match', None)
      if exclude_paths:
        exclude_paths_re = re.compile(exclude_paths)
      else:
        exclude_paths_re = None
      if suppress_if_match == 'yes':
        self._last_group_re.append((group, re.compile(for_paths),
                                    exclude_paths_re, True, params))
      else:
        self._group_re.append((group, re.compile(for_paths),
                               exclude_paths_re, False, params))

    # after all the groups are done, add in the default group
    try:
      self._last_group_re.append((None,
                             re.compile(self.defaults.for_paths),
                             None, False,
                             default_params))
    except AttributeError:
      # there is no self.defaults.for_paths
      pass

    self._group_re = self._group_re + self._last_group_re

  def which_groups(self, path):
    "Return the path's associated groups."
    groups = []
    seen = False
    for group, pattern, exclude_pattern, ignore, repos_params in self._group_re:
      if ignore and seen:
        continue
      match = pattern.match(path)
      if match:
        if exclude_pattern and exclude_pattern.match(path):
          continue
        seen = True
        params = repos_params.copy()
        params.update(match.groupdict())
        groups.append((group, params))
    if not groups:
      groups.append((None, self._global_params))
    return groups


class _sub_section:
  pass


class MissingConfig(Exception):
  pass

class UnknownSubcommand(Exception):
  pass


# enable True/False in older vsns of Python
try:
  _unused = True
except NameError:
  True = 1
  False = 0


if __name__ == '__main__':
  def usage():
    sys.stderr.write(
'''USAGE: %s commit     REPOS-DIR REVISION [CONFIG-FILE]
       %s propchange REPOS-DIR REVISION AUTHOR PROPNAME [CONFIG-FILE]
'''
                     % (sys.argv[0], sys.argv[0]))
    sys.exit(1)

  if len(sys.argv) < 4:
    usage()

  cmd = sys.argv[1]
  repos_dir = sys.argv[2]
  revision = int(sys.argv[3])
  config_fname = None
  author = None
  propname = None

  if cmd == 'commit':
    if len(sys.argv) > 5:
      usage()
    if len(sys.argv) > 4:
      config_fname = sys.argv[4]
  elif cmd == 'propchange':
    if len(sys.argv) < 6 or len(sys.argv) > 7:
      usage()
    author = sys.argv[4]
    propname = sys.argv[5]
    if len(sys.argv) > 6:
      config_fname = sys.argv[6]
  else:
    usage()

  if config_fname is None:
    # default to REPOS-DIR/conf/mailer.conf
    config_fname = os.path.join(repos_dir, 'conf', 'mailer.conf')
    if not os.path.exists(config_fname):
      # okay. look for 'mailer.conf' as a sibling of this script
      config_fname = os.path.join(os.path.dirname(sys.argv[0]), 'mailer.conf')

  if not os.path.exists(config_fname):
    raise MissingConfig(config_fname)

  ### run some validation on these params
  svn.core.run_app(main, cmd, config_fname, repos_dir, revision,
                   author, propname)

# ------------------------------------------------------------------------
# TODO
#
# * add configuration options
#   - default options  [DONE]
#   - per-group overrides  [DONE]
#   - group selection based on repos and on path  [DONE]
#   - each group defines delivery info:
#     o how to construct From:  [DONE]
#     o how to construct To:  [DONE]
#     o subject line prefixes  [DONE]
#     o whether to set Reply-To and/or Mail-Followup-To
#       (btw: it is legal do set Reply-To since this is the originator of the
#        mail; i.e. different from MLMs that munge it)
#   - each group defines content construction:
#     o max size of diff before trimming
#     o max size of entire commit message before truncation
#     o flag to disable generation of add/delete diffs
#   - per-repository configuration
#     o extra config living in repos
#     o how to construct a ViewCVS URL for the diff
#     o optional, non-mail log file
#     o look up authors (username -> email; for the From: header) in a
#       file(s) or DBM
#   - put the commit author into the params dict  [DONE]
#   - if the subject line gets too long, then trim it. configurable?
# * get rid of global functions that should properly be class methods
