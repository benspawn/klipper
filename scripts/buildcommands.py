#!/usr/bin/env python2
# Script to handle build time requests embedded in C code.
#
# Copyright (C) 2016  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import sys, os, subprocess, optparse, logging, shlex, socket, time, traceback
import json, zlib
sys.path.append('./klippy')
import msgproto

FILEHEADER = """
/* DO NOT EDIT!  This is an autogenerated file.  See scripts/buildcommands.py. */

#include "board/irq.h"
#include "board/pgm.h"
#include "command.h"
#include "compiler.h"
"""

def error(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(-1)

Handlers = []


######################################################################
# C call list generation
######################################################################

# Create dynamic C functions that call a list of other C functions
class HandleCallList:
    def __init__(self):
        self.call_lists = {'ctr_run_initfuncs': []}
        self.ctr_dispatch = { '_DECL_CALLLIST': self.decl_calllist }
    def decl_calllist(self, req):
        funcname, callname = req.split()[1:]
        self.call_lists.setdefault(funcname, []).append(callname)
    def update_data_dictionary(self, data):
        pass
    def generate_code(self):
        code = []
        for funcname, funcs in self.call_lists.items():
            func_code = ['    extern void %s(void);\n    %s();' % (f, f)
                         for f in funcs]
            if funcname == 'ctr_run_taskfuncs':
                func_code = ['    irq_poll();\n' + fc for fc in func_code]
            fmt = """
void
%s(void)
{
    %s
}
"""
            code.append(fmt % (funcname, "\n".join(func_code).strip()))
        return "".join(code)

Handlers.append(HandleCallList())


######################################################################
# Static string generation
######################################################################

STATIC_STRING_MIN = 2

# Generate a dynamic string to integer mapping
class HandleStaticStrings:
    def __init__(self):
        self.static_strings = []
        self.ctr_dispatch = { '_DECL_STATIC_STR': self.decl_static_str }
    def decl_static_str(self, req):
        msg = req.split(None, 1)[1]
        self.static_strings.append(msg)
    def update_data_dictionary(self, data):
        data['static_strings'] = { i + STATIC_STRING_MIN: s
                                   for i, s in enumerate(self.static_strings) }
    def generate_code(self):
        code = []
        for i, s in enumerate(self.static_strings):
            code.append('    if (__builtin_strcmp(str, "%s") == 0)\n'
                        '        return %d;\n' % (s, i + STATIC_STRING_MIN))
        fmt = """
uint8_t __always_inline
ctr_lookup_static_string(const char *str)
{
    %s
    return 0xff;
}
"""
        return fmt % ("".join(code).strip(),)

Handlers.append(HandleStaticStrings())


######################################################################
# Constants
######################################################################

# Allow adding build time constants to the data dictionary
class HandleConstants:
    def __init__(self):
        self.constants = {}
        self.ctr_dispatch = { '_DECL_CONSTANT': self.decl_constant }
    def decl_constant(self, req):
        name, value = req.split()[1:]
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if name in self.constants and self.constants[name] != value:
            error("Conflicting definition for constant '%s'" % name)
        self.constants[name] = value
    def update_data_dictionary(self, data):
        data['config'] = self.constants
    def generate_code(self):
        return ""

Handlers.append(HandleConstants())


######################################################################
# Wire protocol commands and responses
######################################################################

# Dynamic command and response registration
class HandleCommandGeneration:
    def __init__(self):
        self.commands = {}
        self.encoders = []
        self.msg_to_id = { m: i for i, m in msgproto.DefaultMessages.items() }
        self.messages_by_name = { m.split()[0]: m for m in self.msg_to_id }
        self.all_param_types = {}
        self.ctr_dispatch = {
            '_DECL_COMMAND': self.decl_command,
            '_DECL_ENCODER': self.decl_encoder,
            '_DECL_OUTPUT': self.decl_output
        }
    def decl_command(self, req):
        funcname, flags, msgname = req.split()[1:4]
        if msgname in self.commands:
            error("Multiple definitions for command '%s'" % msgname)
        self.commands[msgname] = (funcname, flags, msgname)
        msg = req.split(None, 3)[3]
        m = self.messages_by_name.get(msgname)
        if m is not None and m != msg:
            error("Conflicting definition for command '%s'" % msgname)
        self.messages_by_name[msgname] = msg
    def decl_encoder(self, req):
        msg = req.split(None, 1)[1]
        msgname = msg.split()[0]
        m = self.messages_by_name.get(msgname)
        if m is not None and m != msg:
            error("Conflicting definition for message '%s'" % msgname)
        self.messages_by_name[msgname] = msg
        self.encoders.append((msgname, msg))
    def decl_output(self, req):
        msg = req.split(None, 1)[1]
        self.encoders.append((None, msg))
    def create_message_ids(self):
        # Create unique ids for each message type
        msgid = max(self.msg_to_id.values())
        for msgname in self.commands.keys() + [m for n, m in self.encoders]:
            msg = self.messages_by_name.get(msgname, msgname)
            if msg not in self.msg_to_id:
                msgid += 1
                self.msg_to_id[msg] = msgid
    def update_data_dictionary(self, data):
        self.create_message_ids()
        messages = { msgid: msg for msg, msgid in self.msg_to_id.items() }
        data['messages'] = messages
        commands = [self.msg_to_id[msg]
                    for msgname, msg in self.messages_by_name.items()
                    if msgname in self.commands]
        data['commands'] = sorted(commands)
        responses = [self.msg_to_id[msg]
                     for msgname, msg in self.messages_by_name.items()
                     if msgname not in self.commands]
        data['responses'] = sorted(responses)
    def build_parser(self, parser, iscmd):
        if parser.name == "#output":
            comment = "Output: " + parser.msgformat
        else:
            comment = parser.msgformat
        params = '0'
        types = tuple([t.__class__.__name__ for t in parser.param_types])
        if types:
            paramid = self.all_param_types.get(types)
            if paramid is None:
                paramid = len(self.all_param_types)
                self.all_param_types[types] = paramid
            params = 'command_parameters%d' % (paramid,)
        out = """
    // %s
    .msg_id=%d,
    .num_params=%d,
    .param_types = %s,
""" % (comment, parser.msgid, len(types), params)
        if iscmd:
            num_args = (len(types) + types.count('PT_progmem_buffer')
                        + types.count('PT_buffer'))
            out += "    .num_args=%d," % (num_args,)
        else:
            max_size = min(msgproto.MESSAGE_MAX,
                           (msgproto.MESSAGE_MIN + 1
                            + sum([t.max_length for t in parser.param_types])))
            out += "    .max_size=%d," % (max_size,)
        return out
    def generate_responses_code(self):
        encoder_defs = []
        output_code = []
        encoder_code = []
        did_output = {}
        for msgname, msg in self.encoders:
            msgid = self.msg_to_id[msg]
            if msgid in did_output:
                continue
            s = msg
            did_output[msgid] = True
            code = ('    if (__builtin_strcmp(str, "%s") == 0)\n'
                    '        return &command_encoder_%s;\n' % (s, msgid))
            if msgname is None:
                parser = msgproto.OutputFormat(msgid, msg)
                output_code.append(code)
            else:
                parser = msgproto.MessageFormat(msgid, msg)
                encoder_code.append(code)
            parsercode = self.build_parser(parser, 0)
            encoder_defs.append(
                "const struct command_encoder command_encoder_%s PROGMEM = {"
                "    %s\n};\n" % (
                    msgid, parsercode))
        fmt = """
%s

const __always_inline struct command_encoder *
ctr_lookup_encoder(const char *str)
{
    %s
    return NULL;
}

const __always_inline struct command_encoder *
ctr_lookup_output(const char *str)
{
    %s
    return NULL;
}
"""
        return fmt % ("".join(encoder_defs).strip(),
                      "".join(encoder_code).strip(),
                      "".join(output_code).strip())
    def generate_commands_code(self):
        cmd_by_id = {
            self.msg_to_id[self.messages_by_name.get(msgname, msgname)]: cmd
            for msgname, cmd in self.commands.items()
        }
        max_cmd_msgid = max(cmd_by_id.keys())
        index = []
        externs = {}
        for msgid in range(max_cmd_msgid+1):
            if msgid not in cmd_by_id:
                index.append(" {\n},")
                continue
            funcname, flags, msgname = cmd_by_id[msgid]
            msg = self.messages_by_name[msgname]
            externs[funcname] = 1
            parser = msgproto.MessageFormat(msgid, msg)
            parsercode = self.build_parser(parser, 1)
            index.append(" {%s\n    .flags=%s,\n    .func=%s\n}," % (
                parsercode, flags, funcname))
        index = "".join(index).strip()
        externs = "\n".join(["extern void "+funcname+"(uint32_t*);"
                             for funcname in sorted(externs)])
        fmt = """
%s

const struct command_parser command_index[] PROGMEM = {
%s
};

const uint8_t command_index_size PROGMEM = ARRAY_SIZE(command_index);
"""
        return fmt % (externs, index)
    def generate_param_code(self):
        sorted_param_types = sorted(
            [(i, a) for a, i in self.all_param_types.items()])
        params = ['']
        for paramid, argtypes in sorted_param_types:
            params.append(
                'static const uint8_t command_parameters%d[] PROGMEM = {\n'
                '    %s };' % (
                    paramid, ', '.join(argtypes),))
        params.append('')
        return "\n".join(params)
    def generate_code(self):
        parsercode = self.generate_responses_code()
        cmdcode = self.generate_commands_code()
        paramcode = self.generate_param_code()
        return paramcode + parsercode + cmdcode

Handlers.append(HandleCommandGeneration())


######################################################################
# Identify data dictionary generation
######################################################################

def build_identify(version, toolstr):
    data = {}
    for h in Handlers:
        h.update_data_dictionary(data)
    data['version'] = version
    data['build_versions'] = toolstr

    # Format compressed info into C code
    data = json.dumps(data)
    zdata = zlib.compress(data, 9)
    out = []
    for i in range(len(zdata)):
        if i % 8 == 0:
            out.append('\n   ')
        out.append(" 0x%02x," % (ord(zdata[i]),))
    fmt = """
// version: %s
// build_versions: %s

const uint8_t command_identify_data[] PROGMEM = {%s
};

// Identify size = %d (%d uncompressed)
const uint32_t command_identify_size PROGMEM
    = ARRAY_SIZE(command_identify_data);
"""
    return data, fmt % (version, toolstr, ''.join(out), len(zdata), len(data))


######################################################################
# Version generation
######################################################################

# Run program and return the specified output
def check_output(prog):
    logging.debug("Running %s" % (repr(prog),))
    try:
        process = subprocess.Popen(shlex.split(prog), stdout=subprocess.PIPE)
        output = process.communicate()[0]
        retcode = process.poll()
    except OSError:
        logging.debug("Exception on run: %s" % (traceback.format_exc(),))
        return ""
    logging.debug("Got (code=%s): %s" % (retcode, repr(output)))
    if retcode:
        return ""
    try:
        return output.decode()
    except UnicodeError:
        logging.debug("Exception on decode: %s" % (traceback.format_exc(),))
        return ""

# Obtain version info from "git" program
def git_version():
    if not os.path.exists('.git'):
        logging.debug("No '.git' file/directory found")
        return ""
    ver = check_output("git describe --always --tags --long --dirty").strip()
    logging.debug("Got git version: %s" % (repr(ver),))
    return ver

def build_version(extra):
    version = git_version()
    if not version:
        version = "?"
    btime = time.strftime("%Y%m%d_%H%M%S")
    hostname = socket.gethostname()
    version = "%s-%s-%s%s" % (version, btime, hostname, extra)
    return version

# Run "tool --version" for each specified tool and extract versions
def tool_versions(tools):
    tools = [t.strip() for t in tools.split(';')]
    versions = ['', '']
    success = 0
    for tool in tools:
        # Extract first line from "tool --version" output
        verstr = check_output("%s --version" % (tool,)).split('\n')[0]
        # Check if this tool looks like a binutils program
        isbinutils = 0
        if verstr.startswith('GNU '):
            isbinutils = 1
            verstr = verstr[4:]
        # Extract version information and exclude program name
        if ' ' not in verstr:
            continue
        prog, ver = verstr.split(' ', 1)
        if not prog or not ver:
            continue
        # Check for any version conflicts
        if versions[isbinutils] and versions[isbinutils] != ver:
            logging.debug("Mixed version %s vs %s" % (
                repr(versions[isbinutils]), repr(ver)))
            versions[isbinutils] = "mixed"
            continue
        versions[isbinutils] = ver
        success += 1
    cleanbuild = versions[0] and versions[1] and success == len(tools)
    return cleanbuild, "gcc: %s binutils: %s" % (versions[0], versions[1])


######################################################################
# Main code
######################################################################

def main():
    usage = "%prog [options] <cmd section file> <output.c>"
    opts = optparse.OptionParser(usage)
    opts.add_option("-e", "--extra", dest="extra", default="",
                    help="extra version string to append to version")
    opts.add_option("-d", dest="write_dictionary",
                    help="file to write mcu protocol dictionary")
    opts.add_option("-t", "--tools", dest="tools", default="",
                    help="list of build programs to extract version from")
    opts.add_option("-v", action="store_true", dest="verbose",
                    help="enable debug messages")

    options, args = opts.parse_args()
    if len(args) != 2:
        opts.error("Incorrect arguments")
    incmdfile, outcfile = args
    if options.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # Parse request file
    ctr_dispatch = { k: v for h in Handlers for k, v in h.ctr_dispatch.items() }
    f = open(incmdfile, 'rb')
    data = f.read()
    f.close()
    for req in data.split('\0'):
        req = req.lstrip()
        if not req:
            continue
        cmd = req.split()[0]
        if cmd not in ctr_dispatch:
            error("Unknown build time command '%s'" % cmd)
        ctr_dispatch[cmd](req)
    # Create identify information
    cleanbuild, toolstr = tool_versions(options.tools)
    version = build_version(options.extra)
    sys.stdout.write("Version: %s\n" % (version,))
    datadict, icode = build_identify(version, toolstr)
    # Write output
    f = open(outcfile, 'wb')
    f.write(FILEHEADER + "".join([h.generate_code() for h in Handlers])
            + icode)
    f.close()

    # Write data dictionary
    if options.write_dictionary:
        f = open(options.write_dictionary, 'wb')
        f.write(datadict)
        f.close()

if __name__ == '__main__':
    main()
