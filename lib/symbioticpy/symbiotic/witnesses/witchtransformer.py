import sys
import clang.cindex
import yaml


class ValidationTransformer:
    def __init__(self, program_file, witness_file, out_program, out_witness):
        self.program_file = program_file
        self.witness_file = witness_file

        self.out_program = out_program
        self.out_witness = out_witness

        with open(self.witness_file, "r") as file:
            self.witness = yaml.safe_load(file)

        with open(self.program_file, "r") as c_file:
            self.c_lines = c_file.readlines()

        # Keep information about the statements and expressions mentioned in the witness:
        self._calls = dict()        # {witness_location : begin_location}
        self._assumptions = dict()  # {witness_location : (begin_location, end_location, add_bracket?)}
        self._branchings = dict()   # {witness_location : (control_expr_begin, control_expr_end, col)}
        self._switches = dict()     # {witness_location : (control_expr_begin, control_expr_end, col)}
        self._target = dict()       # {witness_location : (begin_location, end_location)}

        # Keep information about what we want to insert into the C code
        # and how the witness locations will change
        self._insert = []  # each item is (line, col, value)
        self._shift = {}  # locations that were shifted to the right

        self.check_witness_structure()

    def check_witness_structure(self):
        # basic syntax checks
        assert len(self.witness) == 1, "Multiple or missing entries in witness!"
        assert "content" in self.witness[0], "Missing witness content!"
        assert "metadata" in self.witness[0], "Missing witness metadata!"
        assert "entry_type" in self.witness[0], "Missing witness entry_type!"
        assert self.witness[0]["entry_type"] == "violation_sequence", "Invalid entry type!"
        assert len(self.witness[0]["content"]) >= 1, "Invalid witness syntax!"

    def _get_witness_locations(self):
        # collect relevant program locations and their types and check basic syntax
        # of the witness

        for s in self.witness[0]["content"]:
            assert "segment" in s, "Invalid witness syntax!"
            segment = s["segment"]

            for w in segment:
                assert "waypoint" in w, "Invalid witness syntax!"
                waypoint = w["waypoint"]

                assert 'location' in waypoint.keys(), "Missing waypoint location!"
                assert 'line' in waypoint['location'].keys(), "Missing line in waypoint location!"
                assert 'type' in waypoint.keys(), "Missing waypoint type!"
                assert 'action' in waypoint.keys(), "Missing waypoint action!"

                if waypoint['action'] == 'follow':
                    assert w == segment[-1]

                line = waypoint['location']['line']
                col = waypoint['location']['column'] if "column" in waypoint['location'] else 0

                if waypoint["type"] == "target":
                    assert waypoint["action"] == "follow"
                    assert s == self.witness[0]["content"][-1]
                    self._target[line, col] = None
                    break

                map = None
                if waypoint["type"] == "function_return" or waypoint["type"] == "function_enter":
                    map = self._calls
                if waypoint["type"] == "assumption":
                    map = self._assumptions
                if waypoint["type"] == "branching":
                    map = self._branchings
                    self._switches[(line, col)] = None

                assert map is not None, "Unknown waypoint type!"

                map[(line, col)] = None

    # Traverse the AST, find the locations mentioned in the witness and store information
    # about them in one of: _calls, _assumptions, _branchings, _target.
    def traverse_AST(self, node):
        # Recurse for children of this node
        child_index = 0
        for child in node.get_children():
            if child.location.file.name != self.program_file:
                continue

            start = child.extent.start
            end = child.extent.end

            # For all function calls and returns, we change the location
            # from the right paranthesis to the position of the call.
            if child.kind == clang.cindex.CursorKind.CALL_EXPR:
                if (end.line, end.column) in self._calls:
                    self._calls[end.line][end.column] = start.line, start.column
                if (end.line, 0) in self._calls and not self._calls[(end.line, 0)]:
                    self._calls[end.line][0] = start.line, start.column

            # For branching waypoints, we find the corresponding control expression
            # and assign an identifier.
            if (start.line, start.column) in self._branchings:
                self._handle_branching(child, (start.line, start.column))
            if (start.line, 0) in self._branchings and not self._branchings[(start.line, 0)]:
                self._handle_branching(child, start.line, 0)

            if child.kind == clang.cindex.CursorKind.CONDITIONAL_OPERATOR:
                self._handle_ternary(child)

            # For assumption waypoint, we check whether they point to a statement and remember the statements range,
            # if necessary
            if (start.line, start.column) in self._assumptions \
                    or (start.line, 0) in self._assumptions and self._assumptions[start.line, 0] is None:
                is_stmt = is_statement(node, child_index, child)

                if is_stmt:
                    brackets = True
                    begin = start.line, start.column
                    end = end.line, end.column
                    if node.kind == clang.cindex.CursorKind.COMPOUND_STMT:
                        brackets = False
                    if child.kind == clang.cindex.CursorKind.COMPOUND_STMT:
                        brackets = False
                        begin = begin[0], begin[1] + 1

                    if (start.line, start.column) in self._assumptions:
                        self._assumptions[start.line, start.column] = begin, end, brackets

                    if (start.line, 0) in self._assumptions and not self._assumptions[start.line, 0]:
                        self._assumptions[start.line, 0] = begin, end, brackets

            # For target, we check that the location points to an expression statement or a full expression
            if (start.line, start.column) in self._target \
                    or ((start.line, 0) in self._target and not self._target[(start.line, 0)]):

                if child.kind.is_expression():
                    col = 0 if (start.line, 0) in self._target else start.column
                    self._target[(start.line, col)] = \
                        (start.line, start.column), (child.extent.end.line, child.extent.end.column)

            child_index += 1

            if not child.kind.is_expression():
                self.traverse_AST(child)

    def _handle_ternary(self, node):
        children = list(node.get_children())
        start = children[0].extent.end
        end = children[1].extent.start

        q_loc = self._find_q_mark(start.line, start.column, end.line, end.column)
        if not q_loc:
            return

        ctrl_expr = children[0] if children[0].kind != clang.cindex.CursorKind.PAREN_EXPR \
            else list(children[0].get_children())[0]

        if q_loc in self._branchings:
            self._add_branchinfo(q_loc[1], q_loc, ctrl_expr.extent)
        if (q_loc[0], 0) in self._branchings:
            self._add_branchinfo(q_loc[1], (q_loc[0], 0), ctrl_expr.extent)

    def _handle_branching(self, node, loc):
        col = node.extent.start.column
        children = list(node.get_children())
        if node.kind == clang.cindex.CursorKind.IF_STMT:
            ctrl_expr = children[0]
            self._add_branchinfo(col, loc, ctrl_expr.extent)

        if node.kind == clang.cindex.CursorKind.WHILE_STMT:
            ctrl_expr = children[0]
            self._add_branchinfo(col, loc, ctrl_expr.extent)

        if node.kind == clang.cindex.CursorKind.DO_STMT:
            ctrl_expr = children[1]
            self._add_branchinfo(col, loc, ctrl_expr.extent)

        if node.kind == clang.cindex.CursorKind.SWITCH_STMT:
            ctrl_expr = children[0]
            self._add_switchinfo(col, loc, ctrl_expr.extent)

    def _add_branchinfo(self, col, loc, extent):
        self._branchings[loc] = (extent.start.line, extent.start.column), \
                                (extent.end.line, extent.end.column), \
                                col

    def _add_switchinfo(self, col, loc, extent):
        self._switches[loc] = (extent.start.line, extent.start.column), \
                                (extent.end.line, extent.end.column), \
                                col

    def transform(self):

        self._get_witness_locations()
        sys.setrecursionlimit(2048)

        index = clang.cindex.Index.create()
        tu = index.parse(self.program_file, args=["-fbracket-depth=2048"])
        root = tu.cursor

        self.traverse_AST(root)

        content = self.witness[0]["content"]
        s_index = 0
        conditions_covered = set()
        for s in content:
            segment = s["segment"]
            for w in segment:
                waypoint = w["waypoint"]

                line = waypoint['location']['line']
                col = waypoint['location']['column']

                if waypoint["type"] == "function_return" or waypoint["type"] == "function_enter":
                    if not self._calls[line, col]:
                        print("Invalid location for function call or return:", loc)
                        continue
                    waypoint['location']['line'], waypoint['location']['column'] = self._call_map[loc]

                if waypoint["type"] == "target":
                    if self._target[line, col] is None:
                        print("Invalid location for target:", line, col)
                        continue

                    waypoint['location']['column'] = self._target[line, col][0][1]
                    waypoint['location2'] = {}
                    waypoint['location2']['line'], waypoint['location2']['column'] = self._target[line, col][1]

                if waypoint["type"] == "assumption":
                    if not self._assumptions[line, col]:
                        print("Invalid location for assumption:", line, col)
                        continue

                    start, end, bracket = self._assumptions[line, col]

                    if bracket:
                        self._insert.append((end[0], end[1] + 1, ';}'))

                    call = create_assumption(waypoint['constraint']['value'],
                                             s_index, waypoint['action'] == 'follow', bracket)
                    self._insert.append((start[0], start[1], call))

                if waypoint["type"] == "branching":

                    if self._branchings[line, col] is not None:
                        ctrl_expr_start, ctrl_expr_end, column = self._branchings[line, col]
                        fun = "__VALIDATOR_branch"
                    elif self._switches[line, col] is not None:
                        ctrl_expr_start, ctrl_expr_end, column = self._switches[line, col]
                        fun = "__VALIDATOR_switch"
                    else:
                        print("Invalid location for branching:", line, col)
                        continue

                    loc = (line, column)
                    if loc not in conditions_covered:
                        self._insert.append((ctrl_expr_start[0], ctrl_expr_start[1],
                                             fun + '(' + str(line) + ', ' + str(column) + ', '))
                        self._insert.append((ctrl_expr_end[0], ctrl_expr_end[1] + 1, ')'))
                        conditions_covered.add(loc)

            s_index += 1

        self._insert_calls()
        self.witness[0]["content"] = self._shift_witness(content)

        with open(self.out_witness, "w") as witness_file2:
            yaml.dump(self.witness, witness_file2, default_style=None)
        with open(self.out_program, "w") as program_file2:
            program_file2.writelines(self.c_lines)

    def _insert_calls(self):
        self._insert.sort(key=lambda item: item[2])
        self._insert.sort(key=lambda item: item[1], reverse = True)
        self._insert.sort(key=lambda item: item[0], reverse = True)

        for line, col, value in self._insert:
            self.c_lines[line - 1] = self.c_lines[line - 1][: col - 1] + value + self.c_lines[line - 1][col - 1:]
            self._add_shift(line, col, len(value))

    def _add_shift(self, line, col, length):
        if line in self._shift and col in self._shift[line]:
            self._shift[line][col] += length
        else:
            self._shift[line] = {}
            self._shift[line][col] = length

    # After we inserted some calls into the C code, some statements described in the witness
    # have changed locations, and we need to adjust it.
    def _shift_witness(self, content):
        for s in content:
            segment = s["segment"]
            for w in segment:
                waypoint = w["waypoint"]

                # We do not care about these locations anymore
                if waypoint["type"] == "assumption" or waypoint["type"] == "branching":
                    continue

                if waypoint["location"]["line"] in self._shift.keys():
                    add = 0
                    for col in self._shift[waypoint["location"]["line"]]:
                        if waypoint["location"]["column"] >= col:
                            add += self._shift[waypoint["location"]["line"]][col]
                    waypoint["location"]["column"] += add

        # Shift end location of target
        target = content[-1]["segment"][-1]["waypoint"]
        if "location2" in target and target["location2"]["line"] in self._shift.keys():
            add = 0
            for col in self._shift[waypoint["location2"]["line"]]:
                if target["location2"]["column"] >= col:
                    add += self._shift[target["location2"]["line"]][col]
                target["location2"]["column"] += add

        return content

    def _find_q_mark(self, startline, startcol, endline, endcol):
        if startline == endline:
            for col in range(startcol, endcol):
                if self.c_lines[startline - 1][col - 1] == '?':
                    return startline, col
        else:
            for col in range(startcol, len(self.c_lines[line - 1])):
                if self.c_lines[startline - 1][col - 1] == '?':
                    return startline, col
            for line in range(startline + 1, endline):
                for col in range(1, len(self.c_lines[line - 1])):
                    if self.c_lines[line - 1][col - 1] == '?':
                        return line, col
            for col in range(1, endcol):
                if self.c_lines[endline - 1][col - 1] == '?':
                    return endline, col

        return None


def create_assumption(constraint, segment, follow, bracket):
    prefix = '{' if bracket else ''
    call_scheme = 'if(__VALIDATOR_segment({seg})) __VALIDATOR_assume({constr}, {foll});'
    call = prefix + call_scheme.format(constr=constraint.strip(';'), seg=segment, foll=1 if follow else 0)
    return call


def is_statement(parent, child_index, child):
    types = clang.cindex.CursorKind

    if parent.kind == types.COMPOUND_STMT or child.is_statement():
        return True

    if (parent.kind == types.WHILE_STMT
        or parent.kind == types.SWITCH_STMT
        or parent.kind == types.FOR_STMT
        or parent.kind == types.CASE_STMT) and \
            child_index == len(parent.get_children()) - 1:
        return True

    if parent.kind == types.IF_STMT and child_index != 0:
        return True

    if parent.kind == types.DO_STMT and child_index == 0:
        return True

    return False

