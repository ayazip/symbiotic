"""
BenchExec is a framework for reliable benchmarking.
This file is part of BenchExec.

Copyright (C) 2007-2015  Dirk Beyer
Copyright (C) 2019-2021  Marek Chalupa
All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import logging
import xml.etree.ElementTree as ET

try:
    import benchexec.util as util
    import benchexec.result as result
    from benchexec.tools.template import BaseTool
except ImportError:
    # fall-back solution (at least for now)
    import symbiotic.benchexec.util as util
    import symbiotic.benchexec.result as result
    from symbiotic.benchexec.tools.template import BaseTool

from symbiotic.utils.process import runcmd
from symbiotic.utils.watch import DbgWatch
from . tool import SymbioticBaseTool

try:
    from symbiotic.versions import llvm_version
except ImportError:
    # the default version
    llvm_version='10.0.1'

class SymbioticTool(BaseTool, SymbioticBaseTool):
    """
    Tool info for CBMC (http://www.cprover.org/cbmc/).
    It always adds --xml-ui to the command-line arguments for easier parsing of
    the output, unless a propertyfile is passed -- in which case running under
    SV-COMP conditions is assumed.
    """

    def __init__(self, opts, only_results=None):
        """ only_results = if not none, report only these results as real,
            otherwise report 'unknown'. Used to implement incremental BMC.
        """
        SymbioticBaseTool.__init__(self, opts)
        opts.explicit_symbolic = True
        self._only_results = only_results

    def executable(self):
        return util.find_executable('cbmc')

    def version(self, executable):
        return self._version_from_tool(executable)

    def name(self):
        return 'CBMC'

    def slicer_options(self):
        """ Override slicer options: do not slice bodies of funs
            that are slicing criteria. CBMC uses the assertions inside,
            not the calls themselves.
        """
        sc, opts = super().slicer_options()
        return (sc, opts + ['--preserved-functions={0}'.format(','.join(sc))])

    def verifiers(self):
        bounds = (2, 6, 12, 17, 21, 40, 200, 400, 1025, 2049, 268435456)
        setups = []
        for b in bounds:
            setups.append((SymbioticTool(self._options, only_results=['false']),
                           ['--unwind', str(b)],
                           None))
            setups.append((SymbioticTool(self._options, only_results=['true']),
                           ['--unwind', str(b), '--unwinding-assertions'],
                           None))
        return setups

    def cmdline(self, executable, options, tasks, propertyfile, rlimits):
       #if propertyfile:
       #    options = options + ['--propertyfile', propertyfile]
       #elif ("--xml-ui" not in options):
       #    options = options + ["--xml-ui"]

        # parameters copied from the SV-COMP wrapper script,
        options.extend(['--stop-on-fail',
                        '--object-bits', '11'])

        prp = self._options.property
        if prp.memsafety() or prp.memcleanup():
            options.extend(['--pointer-check',
                            '--memory-leak-check',
                            '--bounds-check',
                            '--no-assertions'])
        elif prp.signedoverflow():
            options.extend(['--signed-overflow-check',
                            '--no-assertions'])

        return [executable] + options + tasks

    def determine_result(self, returncode, returnsignal, output, isTimeout):
        if returnsignal == 0 and ((returncode == 0) or (returncode == 10)):
            prp = self._options.property
            status = result.RESULT_ERROR
            for line in output:
                line = str(line.strip())
                if "Unmodelled library functions have been called" in line:
                    status = result.RESULT_UNKNOWN
                elif "__CPROVER_memory_leak" in line or\
                     "allocated memory never freed" in line:
                    status = result.RESULT_FALSE_MEMTRACK\
                            if prp.memsafety() else result.RESULT_FALSE_MEMCLEANUP
                elif "double free" in line or\
                     "free called for stack-allocated object" in line or\
                     "free argument" in line:
                    status = result.RESULT_FALSE_FREE
                elif "dereference failure" in line or\
                     "bound in" in line or\
                     "source region" in line:
                    status = result.RESULT_FALSE_DEREF
                elif "arithmetic overflow on signed" in line:
                    status = result.RESULT_FALSE_OVERFLOW
                elif "VERIFICATION SUCCESSFUL" in line:
                    # sanity check
                    if status != result.RESULT_ERROR:
                        # we found some error and yet we were successful?
                        status = 'PARSING FAILED'
                    else:
                        status = result.RESULT_TRUE_PROP
                elif "VERIFICATION FAILED" in line:
                    if self._options.property.termination():
                        status = result.RESULT_FALSE_TERMINATION
                    elif self._options.property.unreachcall():
                        status = result.RESULT_FALSE_REACH
                    # sanity check
                    sw = status.lower().startswith
                    if not (sw('false') or sw('unkown')):
                        status = 'PARSING FAILED'

        elif returncode == 64 and 'Usage error!\n' in output:
            status = 'INVALID ARGUMENTS'

        elif returncode == 6 and 'Out of memory\n' in output:
            status = 'OUT OF MEMORY'

        else:
            status = result.RESULT_ERROR

        if self._only_results:
            res = status.lower()
            if res.startswith('false'):
                res = 'false'
            elif res.startswith('true'):
                res = 'true'
            if not res in self._only_results:
                return result.RESULT_UNKNOWN

        return status

    def llvm_version(self):
        """
        Return required version of LLVM
        """
        return llvm_version

    def set_environment(self, symbiotic_dir, opts):
        """
        Set environment for the tool
        """
        # do not link any functions
        opts.linkundef = []

    def passes_before_verification(self):
        """
        Passes that should run before CPAchecker
        """
        # LLVM backend in CPAchecker does not handle switches correctly yet
        return super().passes_before_verification() + ["-reg2mem", "-lowerswitch", "-simplifycfg"]

    def actions_before_verification(self, symbiotic):
        # link our specific funs
        self._options.linkundef = ['verifier']
        symbiotic.link_undefined(only_func=['__VERIFIER_silent_exit'])
        self._options.linkundef = []
        # translate to C
        output = symbiotic.curfile + '.c'
        runcmd(['llvm2c', symbiotic.curfile, '--add-includes', '--o', output],
                DbgWatch('all'))
        symbiotic.curfile = output
