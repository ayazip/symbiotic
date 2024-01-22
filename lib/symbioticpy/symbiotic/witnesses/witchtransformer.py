import re
import subprocess
import yaml


class ValidationTransformer:
    def __init__(self, program, witness, out_program, out_witness):
        self.program = program
        self.c_lines = []
        self.witness = witness
        self._stmt_map = {}
        self._call_map = {}
        self._cond_map = {}
        self._strings = {}
        self._branch_shift = {}
        self._insert = []
        self._shift = {}

        self.out_program = out_program
        self.out_witness = out_witness

    def parse_ast(self, ast):
        get_expr_after = []
        this_file = True
        last_ln = "0"
        lines = ast.splitlines()

        for i in range(len(lines)):
            line = lines[i]
            location, this_file, last_ln = self.get_location(line, this_file, last_ln)

            if not this_file:
                continue

            start_location = (location["start_ln"], location["start_col"])
            end_location = (location["end_ln"], location["end_col"])

            # index callmap by end location
            if "CallExpr" in line:
                if end_location not in self._call_map:
                    self._call_map[end_location] = start_location

            cond_location = None

            if "WhileStmt" in line:
                cond_location, _, _ = self.get_location(lines[i + 1], this_file, last_ln)

            if "IfStmt" in line:
                cond_location, _, _ = self.get_location(lines[i + 1], this_file, last_ln)
                self._branch_shift[start_location] = cond_location["start_ln"], cond_location["start_col"]

            if "DoStmt" in line:
                do_location, _, _ = self.get_location(lines[i + 1], this_file, last_ln)
                self._branch_shift[start_location] = do_location["end_ln"], do_location["end_col"]
                get_expr_after.append((do_location["end_ln"], do_location["end_col"], location))

            if get_expr_after and location["start_ln"] and (location["start_ln"] > get_expr_after[-1][0] or
                                   (location["start_ln"] == get_expr_after[-1][0] and location["start_col"] >
                                    get_expr_after[-1][1])):
                cond_location = location
                _, _, location = get_expr_after.pop()

            if cond_location:
                self._cond_map[(location["start_ln"], location["start_col"])] = \
                    (cond_location["start_ln"], cond_location["start_col"],
                     cond_location["end_ln"], cond_location["end_col"])

            # index statement map by begin location
            if start_location not in self._stmt_map:
                self._stmt_map[start_location] = end_location

    def get_location(self, line, correct_file, last_ln):
        location = {"start_ln": None, "start_col": None, "end_ln": None, "end_col": None}

        # check if we have a begin and end
        loc = re.search("^[^-]*-[a-zA-Z]* 0x[0-9A-Fa-f]* <([^>,]*), ([^,]*)>", line)

        if not loc:  # if not, we only care about the start
            loc = re.search("^[^-]*-[a-zA-Z]* 0x[0-9A-Fa-f]* <line:([0-9]*):([0-9]*)>", line) 
            if loc:
                location["start_col"] = int(loc[2])
                location["end_col"] = int(loc[2])
            else:
                loc = re.search("^[^-]*-[a-zA-Z]* 0x[0-9A-Fa-f]* <line:([0-9]*)", line)
            if loc:
                location["start_ln"] = int(loc[1])
                location["end_ln"] = int(loc[1])
                last_ln = int(loc[1])
            return location, correct_file, last_ln

        start = loc[1]
        end = loc[2]

        begin = re.search("([^:]*):([0-9]*):([0-9]*)", start)
        if not begin:  # No filename and line is specified
            if not correct_file:
                return location, correct_file, last_ln

            name = None
            # if there's no line specified, use the previous one
            location["start_ln"] = last_ln
            location["start_col"] = int(re.search("col:([0-9]*)", start)[1])
        else:  # There is possibly a filename, a line and a column
            name = begin[1]
            location["start_ln"] = int(begin[2])
            location["start_col"] = int(begin[3])

        # if there is a filename, check if its the program under validation - we do not care about headers
        if name and name != "line":
            if name != self.program:
                correct_file = False
                return location, correct_file, last_ln
            else:
                correct_file = True

        # update last line
        last_ln = location["start_ln"]

        # again, check if there's a line number, otherwise use previous
        if "line" in end:
            end_loc = re.search("line:([0-9]*):([0-9]*)", end)
            location["end_ln"], location["end_col"] = int(end_loc[1]), int(end_loc[2])
            last_ln = location["end_ln"]
        else:
            location["end_ln"] = last_ln
            location["end_col"] = int(re.search("col:([0-9]*)", end)[1])

        last = re.search(".*<.*> line:([0-9]*)", line)
        if last:
            last_ln = int(last[1])

        return location, correct_file, last_ln

    def transform(self):
        conditions_covered = set()
        with open(self.witness, "r") as witness_file, \
                open(self.program, "r") as c_file:

            self.c_lines = c_file.readlines()
            ast = subprocess.run(['clang', '-Xclang', '-ast-dump', '-fsyntax-only', 
                                  '-fbracket-depth=-1', '-fno-color-diagnostics',
                                  self.program],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode('utf-8')
            self.parse_ast(ast)
            witness = yaml.safe_load(witness_file)
            content = witness[0]["content"]
            s_index = 0

            for s in content:
                segment = s["segment"]
                for w in segment:
                    waypoint = w["waypoint"]
                    assert 'location' in waypoint.keys(), "Location must be specified!"
                    assert 'line' in waypoint['location'].keys(), "Line must be specified!"
                    assert 'column' in waypoint['location'].keys(), "Currently, Witch requires the column number"
   
                    loc = (waypoint['location']['line'], waypoint['location']['column'])

                    if waypoint["type"] == "function_return" or waypoint["type"] == "function_enter":
                        if loc not in self._call_map:
                            print("Invalid location for function call:", loc)
                            continue
                        waypoint['location']['line'], waypoint['location']['column'] = self._call_map[loc]

                    if waypoint["type"] == "target":
                        if loc not in self._stmt_map:
                            print("Invalid location for statement begin:", loc)
                            continue
                        waypoint['location2'] = {}
                        waypoint['location2']['line'], waypoint['location2']['column'] = self._stmt_map[loc]

                    if waypoint["type"] == "assumption":
                        if loc not in self._stmt_map:
                            print("Invalid location for assumption:", loc)
                            continue
                        prefix = ''
                        if last_char(loc[0] - 1, loc[1] - 2, self.c_lines) == ')':
                            prefix = '{'
                            expr_end = self._stmt_map[(loc[0], loc[1])]
                            end = get_end(expr_end[0], expr_end[1], self.c_lines, ';')
                            self._insert.append((end[0], end[1] + 1, '}', None))

                        self._insert.append((waypoint['location']['line'],
                                             waypoint['location']['column'],
                                             'assumption',
                                             (waypoint['constraint']['value'], s_index,
                                              waypoint['action'] == 'follow', prefix)))

                    if waypoint["type"] == "branching":
                        if loc not in self._cond_map:
                            print("Invalid location for branching:", loc)
                            continue
                        cond_loc = self._cond_map[loc]
                        if cond_loc not in conditions_covered:
                            self._insert.append((cond_loc[0], cond_loc[1], "__VALIDATOR_branch(", None))
                            end = get_end(cond_loc[2], cond_loc[3], self.c_lines, ')')
                            self._insert.append((end[0], end[1] + 1, ")", None))
                            conditions_covered.add(cond_loc)
                        if loc in self._branch_shift:
                            waypoint['location']['line'], waypoint['location']['column'] = self._branch_shift[loc]

                s_index += 1

            self.insert()
            witness[0]["content"] = self.shift_witness(content)

            with open(self.out_witness, "w") as witness_file2:
                yaml.dump(witness, witness_file2, default_style=None)
            with open(self.out_program, "w") as program_file2:
                program_file2.writelines(self.c_lines)
            


    def insert(self):
        self._insert.sort(reverse=True)
        for line, col, value, assume in self._insert:

            if value != "assumption":
                self.c_lines[line - 1] = self.c_lines[line - 1][: col - 1] + value + self.c_lines[line - 1][col - 1:]
                if value == "__VALIDATOR_branch(":
                    self.add_shift(line, col + 1, len(value))
                else:
                    self.add_shift(line, col, len(value))
                continue

            constraint, segment, follow, prefix = assume

            calltext = "__VALIDATOR_assume({constr}, {seg}, {foll}); "
            call = prefix + calltext.format(constr=constraint.strip(';'), seg=segment, foll=1 if follow else 0)
            self.c_lines[line - 1] = self.c_lines[line - 1][: col - 1] + call + self.c_lines[line - 1][col - 1:]
            self.add_shift(line, col, len(call))

    def add_shift(self, line, col, length):
        if line in self._shift and col in self._shift[line]:
            self._shift[line][col] += length
        else:
            self._shift[line] = {}
            self._shift[line][col] = length

    def shift_witness(self, content):
        for s in content:
            segment = s["segment"]
            for w in segment:
                waypoint = w["waypoint"]

                if waypoint["type"] == "assumption":
                    continue

                if waypoint["location"]["line"] in self._shift.keys():
                    add = 0
                    for col in self._shift[waypoint["location"]["line"]]:
                        if waypoint["location"]["column"] >= col:
                            add += self._shift[waypoint["location"]["line"]][col]
                    waypoint["location"]["column"] += add

        target = content[-1]["segment"][-1]["waypoint"]
        if "location2" in target and target["location2"]["line"] in self._shift.keys():
            add = 0
            for col in self._shift[waypoint["location2"]["line"]]:
                if target["location2"]["column"] >= col:
                    add += self._shift[target["location2"]["line"]][col]
                target["location2"]["column"] += add

        return content


def last_char(line, col, lines):
    while line >= 0 and lines[line][col].isspace():
        if col > 0:
            col -= 1
        else:
            line -= 1
            col = len(lines[line]) - 1
        x = lines[line][col]
    return lines[line][col] if line > 0 and col > 0 else None


def get_char(line, col, lines):
    return lines[line - 1][col - 1]

# Horrible hack. Doesn't consider char of string literals  
def get_end(line, col, lines, marker):
    while line - 1 < len(lines) and lines[line - 1][col - 1] != marker:
        if col - 1 == len(lines[line - 1]) - 1:
            line += 1
        else:
            col += 1
    return line, col if line != len(lines) - 1 else None

