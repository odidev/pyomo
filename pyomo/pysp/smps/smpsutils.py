#  _________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright (c) 2014 Sandia Corporation.
#  Under the terms of Contract DE-AC04-94AL85000 with Sandia Corporation,
#  the U.S. Government retains certain rights in this software.
#  This software is distributed under the BSD License.
#  _________________________________________________________________________

import os
import operator
import shutil
import filecmp
import logging
import itertools
from collections import namedtuple

from pyomo.opt import WriterFactory
from pyomo.core.base.numvalue import value
from pyomo.core.base.block import (Block,
                                   _BlockData,
                                   SortComponents)
from pyomo.core.base.var import Var, _VarData
from pyomo.core.base.constraint import Constraint, _ConstraintData
from pyomo.core.base.sos import SOSConstraint
from pyomo.core.base.suffix import ComponentMap
from pyomo.repn import LinearCanonicalRepn
from pyomo.repn import generate_canonical_repn
from pyomo.pysp.scenariotree.manager import InvocationType
from pyomo.pysp.embeddedsp import EmbeddedSP
from pyomo.pysp.annotations import (locate_annotations,
                                    _ConstraintStageAnnotation,
                                    StochasticConstraintBoundsAnnotation,
                                    StochasticConstraintBodyAnnotation,
                                    StochasticObjectiveAnnotation,
                                    StochasticVariableBoundsAnnotation)

from six import iteritems, itervalues

thisfile = os.path.abspath(__file__)

logger = logging.getLogger('pyomo.pysp')

# LONG TERM TODO:
#  - Multi-stage?
#  - Quadratic constraints and objectives?
#     - For variables with both linear and quadratic terms, how
#       to distinguish between the two with model annotations?

_deterministic_check_value = -99999999

def _safe_remove_file(filename):
    """Try to remove a file, ignoring failure."""
    try:
        os.remove(filename)
    except OSError:
        pass

def _no_negative_zero(val):
    """Make sure -0 is never output. Makes diff tests easier."""
    if val == 0:
        return 0
    return val

ProblemStats = namedtuple("ProblemStats",
                          ["firststage_variable_count",
                           "secondstage_variable_count",
                           "firststage_constraint_count",
                           "secondstage_constraint_count",
                           "stochastic_cost_count",
                           "stochastic_rhs_count",
                           "stochastic_matrix_count",
                           "scenario_count"])

def map_constraint_stages(scenario,
                          scenario_tree,
                          symbol_map,
                          stochastic_constraint_ids,
                          firststage_variable_ids,
                          secondstage_variable_ids):

    reference_model = scenario._instance

    assert len(scenario_tree.stages) == 2
    firststage = scenario_tree.stages[0]
    secondstage = scenario_tree.stages[1]

    rootnode = scenario_tree.findRootNode()
    assert len(scenario_tree.stages) == 2

    StageToConstraintMap = {}
    StageToConstraintMap[firststage.name] = []
    StageToConstraintMap[secondstage.name] = []

    #
    # Keep output deterministic, there is enough to deal
    # with already
    #
    sortOrder = SortComponents.indices | SortComponents.alphabetical

    byObject = symbol_map.byObject
    # deal with the fact that the LP/MPS writer prepends constraint
    # names with things like 'c_e_', 'c_l_', etc depending on the
    # constraint bound type and will even split a constraint into
    # two constraints if it has two bounds
    reverse_alias = \
        dict((symbol, []) for symbol in symbol_map.bySymbol)
    for alias, obj_weakref in iteritems(symbol_map.aliases):
        reverse_alias[byObject[id(obj_weakref())]].append(alias)

    # ** SORT POINT TO AVOID NON-DETERMINISTIC ROW ORDERING ***
    for _aliases in itervalues(reverse_alias):
        _aliases.sort()

    #
    # Loop through constraints
    #
    for block in reference_model.block_data_objects(
            active=True,
            descend_into=True,
            sort=sortOrder):

        for con in block.component_data_objects(
                SOSConstraint,
                active=True,
                descend_into=False):
            raise TypeError("SOSConstraints are not allowed with this format. "
                            "Invalid constraint: %s"
                            % (con.cname(True)))

        block_canonical_repn = getattr(block, "_canonical_repn", None)
        if block_canonical_repn is None:
            raise ValueError(
                "Unable to find _canonical_repn ComponentMap "
                "on block %s" % (block.cname(True)))

        for con in block.component_data_objects(
                Constraint,
                active=True,
                descend_into=False,
                sort=sortOrder):

            symbol = byObject[id(con)]
            # if it is a range constraint, this will account for
            # that fact and store an alias for each bound
            aliases = reverse_alias[symbol]
            assert len(aliases) > 0

            if id(con) in stochastic_constraint_ids:
                # there is stochastic data in this constraint
                constraint_stage = secondstage
            else:
                # Note: By the time we get to this function,
                #       there is no concept of derived stage
                #       variables. They have either been pushed
                #       into the leaf-stage or have been
                #       re-categorized as standard variables
                #       where non-anticipativity will be enforced.
                constraint_stage = firststage
                for var in EmbeddedSP._collect_variables(con.body).values():
                    if not var.fixed:
                        if id(var) in secondstage_variable_ids:
                            constraint_stage = secondstage
                            break
                        else:
                            assert id(var) in firststage_variable_ids

            StageToConstraintMap[constraint_stage.name].\
                append((aliases, con))

    assert sorted(StageToConstraintMap.keys()) == \
        sorted([firststage.name, secondstage.name])

    # sort each by name
    for key in StageToConstraintMap:
        StageToConstraintMap[key].sort(key=operator.itemgetter(0))

    return StageToConstraintMap

def map_variable_stages(scenario,
                        scenario_tree,
                        symbol_map,
                        enforce_derived_nonanticipativity=False):

    reference_model = scenario._instance

    FirstStageVars = {}
    SecondStageVars = {}

    rootnode = scenario_tree.findRootNode()
    assert len(scenario_tree.stages) == 2
    firststage = scenario_tree.stages[0]
    secondstage = scenario_tree.stages[1]
    secondstage_node = scenario.node_list[-1]
    assert secondstage_node.stage is secondstage
    firststage_standard_variables = rootnode._standard_variable_ids
    firststage_derived_variables = rootnode._derived_variable_ids
    secondstage_variables = secondstage_node._variable_ids

    scenariotree_byObject = reference_model._ScenarioTreeSymbolMap.byObject
    symbolmap_byObject = symbol_map.byObject
    for var in reference_model.component_data_objects(
            Var,
            descend_into=True):
        if id(var) not in symbolmap_byObject:
            continue
        symbol = symbolmap_byObject[id(var)]
        scenario_tree_id = scenariotree_byObject.get(id(var), None)
        if scenario_tree_id in firststage_standard_variables:
            FirstStageVars[symbol] = (var, scenario_tree_id)
        elif enforce_derived_nonanticipativity and \
             (scenario_tree_id in firststage_derived_variables):
            FirstStageVars[symbol] = (var, scenario_tree_id)
        elif (scenario_tree_id in firststage_derived_variables) or \
             (scenario_tree_id in secondstage_variables) or \
             (scenario_tree_id is None):
            SecondStageVars[symbol] = (var, scenario_tree_id)
        else:
            # More than two stages?
            assert False

    StageToVariableMap = {}
    StageToVariableMap[firststage.name] = \
        [(symbol,
          FirstStageVars[symbol][0],
          FirstStageVars[symbol][1])
         for symbol in sorted(FirstStageVars)]
    StageToVariableMap[secondstage.name] = \
        [(symbol,
          SecondStageVars[symbol][0],
          SecondStageVars[symbol][1])
         for symbol in sorted(SecondStageVars)]

    return StageToVariableMap

def _convert_external_setup(worker, scenario, *args, **kwds):
    reference_model = scenario._instance
    #
    # We will be tweaking the canonical_repn objects on objectives
    # and constraints, so cache anything related to this here so
    # that this function does not have any side effects on the
    # instance after returning
    #
    cached_attrs = []
    for block in reference_model.block_data_objects(
            active=True,
            descend_into=True):
        block_cached_attrs = {}
        if hasattr(block, "_gen_obj_canonical_repn"):
            block_cached_attrs["_gen_obj_canonical_repn"] = \
                block._gen_obj_canonical_repn
            del block._gen_obj_canonical_repn
        if hasattr(block, "_gen_con_canonical_repn"):
            block_cached_attrs["_gen_con_canonical_repn"] = \
                block._gen_con_canonical_repn
            del block._gen_con_canonical_repn
        if hasattr(block, "_canonical_repn"):
            block_cached_attrs["_canonical_repn"] = \
                block._canonical_repn
            del block._canonical_repn
        cached_attrs.append((block, block_cached_attrs))

    try:
        return _convert_external_setup_without_cleanup(
            worker, scenario, *args, **kwds)
    except:
        logger.error("Failed to complete partial SMPS conversion "
                     "for scenario: %s" % (scenario.name))
        raise
    finally:
        for block, block_cached_attrs in cached_attrs:
            for name in block_cached_attrs:
                setattr(block, name, block_cached_attrs[name])

def _convert_external_setup_without_cleanup(
        worker,
        scenario,
        output_directory,
        basename,
        file_format,
        enforce_derived_nonanticipativity,
        io_options):
    import pyomo.environ
    assert os.path.exists(output_directory)
    assert file_format in ('lp', 'mps')

    io_options = dict(io_options)
    scenario_tree = worker.scenario_tree
    reference_model = scenario._instance
    firststage = scenario_tree.stages[0]
    secondstage = scenario_tree.stages[1]
    constraint_name_buffer = {}
    objective_name_buffer = {}
    variable_name_buffer = {}

    all_constraints = list(
        con for con in reference_model.component_data_objects(
            Constraint,
            active=True,
            descend_into=True))

    #
    # Check for model annotations
    #
    stochastic_rhs = locate_annotations(
        reference_model,
        StochasticConstraintBoundsAnnotation,
        max_allowed=1)
    if len(stochastic_rhs) == 0:
        stochastic_rhs = None
        stochastic_rhs_entries = {}
        empty_rhs_annotation = False
    else:
        assert len(stochastic_rhs) == 1
        stochastic_rhs = stochastic_rhs[0][1]
        if stochastic_rhs.has_declarations():
            empty_rhs_annotation = False
            stochastic_rhs_entries = stochastic_rhs.expand_entries()
            stochastic_rhs_entries.sort(
                key=lambda x: x[0].cname(True, constraint_name_buffer))
            if len(stochastic_rhs_entries) == 0:
                raise RuntimeError(
                    "The %s annotation was declared "
                    "with explicit entries but no active Constraint "
                    "objects were recovered from those entries."
                    % (StochasticConstraintBoundsAnnotation.__name__))
        else:
            empty_rhs_annotation = True
            stochastic_rhs_entries = tuple((con, stochastic_rhs.default)
                                           for con in all_constraints)


    stochastic_matrix = locate_annotations(
        reference_model,
        StochasticConstraintBodyAnnotation,
        max_allowed=1)
    if len(stochastic_matrix) == 0:
        stochastic_matrix = None
        stochastic_matrix_entries = {}
        empty_matrix_annotation = False
    else:
        assert len(stochastic_matrix) == 1
        stochastic_matrix = stochastic_matrix[0][1]
        if stochastic_matrix.has_declarations():
            empty_matrix_annotation = False
            stochastic_matrix_entries = stochastic_matrix.expand_entries()
            stochastic_matrix_entries.sort(
                key=lambda x: x[0].cname(True, constraint_name_buffer))
            if len(stochastic_matrix_entries) == 0:
                raise RuntimeError(
                    "The %s annotation was declared "
                    "with explicit entries but no active Constraint "
                    "objects were recovered from those entries."
                    % (StochasticConstraintBoundsAnnotation.__name__))
        else:
            empty_matrix_annotation = True
            stochastic_matrix_entries = tuple((con,stochastic_matrix.default)
                                              for con in all_constraints)

    stochastic_constraint_ids = set()
    stochastic_constraint_ids.update(id(con) for con,_
                                     in stochastic_rhs_entries)
    stochastic_constraint_ids.update(id(con) for con,_
                                     in stochastic_matrix_entries)

    stochastic_objective = locate_annotations(
        reference_model,
        StochasticObjectiveAnnotation,
        max_allowed=1)
    if len(stochastic_objective) == 0:
        stochastic_objective = None
    else:
        assert len(stochastic_objective) == 1
        stochastic_objective = stochastic_objective[0][1]

    stochastic_varbounds = locate_annotations(
        reference_model,
        StochasticVariableBoundsAnnotation)
    if len(stochastic_varbounds) > 0:
        raise ValueError(
            "The SMPS writer does not currently support "
            "stochastic variable bounds. Invalid annotation type: %s"
            % (StochasticVariableBoundsAnnotation.__name__))

    if (stochastic_rhs is None) and \
       (stochastic_matrix is None) and \
       (stochastic_objective is None):
        raise RuntimeError(
            "No stochastic annotations found. SMPS "
            "conversion requires at least one of the following "
            "annotation types:\n - %s\n - %s\n - %s"
            % (StochasticConstraintBoundsAnnotation.__name__,
               StochasticConstraintBodyAnnotation.__name__,
               StochasticObjectiveAnnotation.__name__))

    #
    # Write the LP/MPS file once to obtain the symbol map
    #
    assert not hasattr(reference_model, "_canonical_repn")
    with WriterFactory(file_format) as writer:
        output_filename = \
            os.path.join(output_directory,
                         basename+".setup."+file_format+"."+scenario.name)
        assert 'column_order' not in io_options
        assert 'row_order' not in io_options
        output_fname, symbol_map = writer(reference_model,
                                          output_filename,
                                          lambda x: True,
                                          io_options)
        assert output_fname == output_filename
    assert hasattr(reference_model, "_canonical_repn")

    StageToVariableMap = map_variable_stages(
        scenario,
        scenario_tree,
        symbol_map,
        enforce_derived_nonanticipativity=enforce_derived_nonanticipativity)
    firststage_variable_ids = \
        set(id(var) for symbol, var, scenario_tree_id
            in StageToVariableMap[firststage.name])
    secondstage_variable_ids = \
        set(id(var) for symbol, var, scenario_tree_id
            in StageToVariableMap[secondstage.name])

    StageToConstraintMap = \
        map_constraint_stages(
            scenario,
            scenario_tree,
            symbol_map,
            stochastic_constraint_ids,
            firststage_variable_ids,
            secondstage_variable_ids)
    secondstage_constraint_ids = \
        set(id(con) for symbols, con
            in StageToConstraintMap[secondstage.name])

    assert len(scenario_tree.stages) == 2
    firststage = scenario_tree.stages[0]
    secondstage = scenario_tree.stages[1]

    # disable these as they do not need to be regenerated and
    # we will be modifiying them
    canonical_repn_cache = {}
    for block in reference_model.block_data_objects(
            active=True,
            descend_into=True):
        canonical_repn_cache[id(block)] = block._canonical_repn
        block._gen_obj_canonical_repn = False
        block._gen_con_canonical_repn = False

    #
    # Make sure the objective references all first stage variables.
    # We do this by directly modifying the canonical_repn of the
    # objective which the LP/MPS writer will reference next time we call
    # it. In addition, make sure that the first second-stage variable
    # in our column ordering also appears in the objective so that
    # ONE_VAR_CONSTANT does not get identified as the first
    # second-stage variable.
    # ** Just do NOT preprocess again until we call the writer **
    #
    objective_object = scenario._instance_objective
    assert objective_object is not None
    objective_block = objective_object.parent_block()
    objective_repn = canonical_repn_cache[id(objective_block)][objective_object]

    #
    # Create column (variable) ordering maps for LP/MPS files
    #
    column_order = ComponentMap()
    # first-stage variables
    for column_index, (symbol, var, scenario_tree_id) \
        in enumerate(StageToVariableMap[firststage.name]):
        column_order[var] = column_index
        if symbol == "RHS":
            raise RuntimeError(
                "Congratulations! You have hit an edge case. The "
                "SMPS input format forbids variables from using "
                "the symbol 'RHS'. Please rename it or use a "
                "different symbol in the output file.")
    # second-stage variables
    for column_index, (symbol, var, scenario_tree_id) \
        in enumerate(StageToVariableMap[secondstage.name],
                     len(column_order)):
        column_order[var] = column_index
        if symbol == "RHS":
            raise RuntimeError(
                "Congratulations! You have hit an edge case. The "
                "SMPS input format forbids variables from using "
                "the symbol 'RHS'. Please rename it or use a "
                "different symbol in the output file.")

    #
    # Create row (constraint) ordering maps for LP/MPS files
    #
    row_order = ComponentMap()
    # first-stage constraints
    for row_index, (symbols, con) \
        in enumerate(StageToConstraintMap[firststage.name]):
        row_order[con] = row_index
    # second-stage constraints
    for row_index, (symbols, con) \
        in enumerate(StageToConstraintMap[secondstage.name],
                     len(row_order)):
        row_order[con] = row_index

    #
    # Write the ordered LP/MPS file
    #
    output_filename = os.path.join(output_directory,
                                   basename+"."+file_format+"."+scenario.name)
    with WriterFactory(file_format) as writer:
        assert 'column_order' not in io_options
        assert 'row_order' not in io_options
        io_options['column_order'] = column_order
        io_options['row_order'] = row_order
        io_options['force_objective_constant'] = True
        output_fname, symbol_map = writer(reference_model,
                                          output_filename,
                                          lambda x: True,
                                          io_options)
        assert output_fname == output_filename

    # re-generate these maps as the LP/MPS symbol map
    # is likely different
    StageToVariableMap = map_variable_stages(
        scenario,
        scenario_tree,
        symbol_map,
        enforce_derived_nonanticipativity=enforce_derived_nonanticipativity)

    StageToConstraintMap = map_constraint_stages(
        scenario,
        scenario_tree,
        symbol_map,
        stochastic_constraint_ids,
        firststage_variable_ids,
        secondstage_variable_ids)

    # generate a few data structures that are used
    # when writing the .sto file
    constraint_symbols = ComponentMap(
        (con, symbols) for stage_name in StageToConstraintMap
        for symbols, con in StageToConstraintMap[stage_name])

    #
    # Write the explicit column ordering (variables) used
    # for the ordered LP/MPS file
    #
    firststage_variable_count = 0
    secondstage_variable_count = 0
    with open(os.path.join(output_directory,
                           basename+".col."+scenario.name),'w') as f_col:
        # first-stage variables
        for (symbol, _, _) in StageToVariableMap[firststage.name]:
            f_col.write(symbol+"\n")
            firststage_variable_count += 1
        # second-stage variables
        for (symbol, _, _) in StageToVariableMap[secondstage.name]:
            f_col.write(symbol+"\n")
            secondstage_variable_count += 1
        f_col.write("ONE_VAR_CONSTANT\n")
        secondstage_variable_count += 1

    #
    # Write the explicit row ordering (constraints) used
    # for the ordered LP/MPS file
    #
    firststage_constraint_count = 0
    secondstage_constraint_count = 0
    with open(os.path.join(output_directory,
                           basename+".row."+scenario.name),'w') as f_row:
        # the objective is always the first row in SMPS format
        f_row.write(symbol_map.byObject[id(objective_object)]+"\n")
        # first-stage constraints
        for (symbols, _) in StageToConstraintMap[firststage.name]:
            # because range constraints are split into two
            # constraints (hopefully our ordering of the r_l_
            # and r_u_ forms is the same as the LP/MPS file!)
            for symbol in symbols:
                f_row.write(symbol+"\n")
                firststage_constraint_count += 1
        # second-stage constraints
        for (symbols, _) in StageToConstraintMap[secondstage.name]:
            # because range constraints are split into two
            # constraints (hopefully our ordering of the r_l_
            # and r_u_ forms is the same as the LP/MPS file!)
            for symbol in symbols:
                f_row.write(symbol+"\n")
                secondstage_constraint_count += 1
        f_row.write("c_e_ONE_VAR_CONSTANT")
        secondstage_constraint_count += 1

    #
    # Write the .tim file
    #
    with open(os.path.join(output_directory,
                           basename+".tim."+scenario.name),'w') as f_tim:
        f_tim.write("TIME %s\n" % (basename))
        if file_format == 'mps':
            f_tim.write("PERIODS IMPLICIT\n")
            f_tim.write("    %s %s TIME1\n"
                        % (StageToVariableMap[firststage.name][0][0],
                           symbol_map.byObject[id(objective_object)]))
            symbols = StageToConstraintMap[secondstage.name][0][0]
            if len(symbols) == 1:
                # equality constraint
                assert (symbols[0].startswith('c_e_') or \
                        symbols[0].startswith('c_l_') or \
                        symbols[0].startswith('c_u_'))
                stage2_row_start = symbols[0]
            else:
                # range constraint (assumed the LP/MPS writer outputs
                # the lower range constraint first)
                symbols = sorted(symbols)
                assert (symbols[0].startswith('r_l_') or \
                        symbols[0].startswith('r_u_'))
                stage2_row_start = symbols[0]
            # don't assume there is always a second stage variable
            if len(StageToVariableMap[secondstage.name][0][0]) > 0:
                f_tim.write("    %s "
                            % (StageToVariableMap[secondstage.name][0][0]))
            else:
                f_tim.write("    ONE_VAR_CONSTANT ")
            f_tim.write("%s TIME2\n" % (stage2_row_start))
        else:
            assert file_format == "lp"
            f_tim.write("PERIODS EXPLICIT\n")
            f_tim.write("    TIME1\n")
            f_tim.write("    TIME2\n")
            line_template = "    %s %s\n"
            f_tim.write("ROWS\n")
            # the objective is always the first row in SMPS format
            f_tim.write(line_template
                        % (symbol_map.byObject[id(objective_object)],
                           "TIME1"))
            # first-stage constraints
            for (symbols, _) in StageToConstraintMap[firststage.name]:
                for symbol in symbols:
                    f_tim.write(line_template % (symbol, "TIME1"))
            # second-stage constraints
            for (symbols, _) in StageToConstraintMap[secondstage.name]:
                for symbol in symbols:
                    f_tim.write(line_template % (symbol, "TIME2"))
            f_tim.write(line_template % ("c_e_ONE_VAR_CONSTANT", "TIME2"))

            f_tim.write("COLS\n")
            # first-stage variables
            for (symbol, _, _) in StageToVariableMap[firststage.name]:
                f_tim.write(line_template % (symbol, "TIME1"))
            # second-stage variables
            for (symbol, _, _) in StageToVariableMap[secondstage.name]:
                f_tim.write(line_template % (symbol, "TIME2"))
            f_tim.write(line_template % ("ONE_VAR_CONSTANT", "TIME2"))

        f_tim.write("ENDATA\n")

    stochastic_lp_labels = set()
    stochastic_constraint_count = 0
    stochastic_secondstage_rhs_count = 0
    stochastic_firststagevar_constraint_count = 0
    stochastic_secondstagevar_constraint_count = 0
    stochastic_firststagevar_objective_count = 0
    stochastic_secondstagevar_objective_count = 0
    #
    # Write the body of the .sto file
    #
    #
    # **NOTE: In the code that follows we assume the LP/MPS
    #         writer always moves constraint body
    #         constants to the rhs and that the lower part
    #         of any range constraints are written before
    #         the upper part.
    #
    modified_constraint_lb = ComponentMap()
    modified_constraint_ub = ComponentMap()
    stochastic_rhs_count = 0
    stochastic_matrix_count = 0
    stochastic_cost_count = 0
    with open(os.path.join(output_directory,
                           basename+".sto.struct."+scenario.name),'w') as f_coords:
        with open(os.path.join(output_directory,
                               basename+".sto."+scenario.name),'w') as f_sto:
            scenario_probability = scenario.probability
            f_sto.write(" BL BLOCK1 PERIOD2 %.17g\n"
                        % (_no_negative_zero(scenario_probability)))

            #
            # Stochastic RHS
            #
            rhs_template = "    RHS    %s    %.17g\n"
            if stochastic_rhs is not None:
                for con, include_bound in stochastic_rhs_entries:
                    assert isinstance(con, _ConstraintData)
                    if not empty_rhs_annotation:
                        # verify that this constraint was
                        # flagged by PySP or the user as second-stage
                        if id(con) not in secondstage_constraint_ids:
                            raise RuntimeError(
                                "The constraint %s has been declared "
                                "in the %s annotation but it was not identified as "
                                "a second-stage constraint. To correct this issue, "
                                "either remove the constraint from this annotation "
                                "or manually declare it as second-stage using the "
                                "%s annotation."
                                % (con.cname(True),
                                   StochasticConstraintBoundsAnnotation.__name__,
                                   ConstraintStageAnnotation.__name__))

                    constraint_repn = \
                        canonical_repn_cache[id(con.parent_block())][con]
                    if not isinstance(constraint_repn, LinearCanonicalRepn):
                        raise RuntimeError("Only linear constraints are "
                                           "accepted for conversion to SMPS format. "
                                           "Constraint %s is not linear."
                                           % (con.cname(True)))

                    body_constant = constraint_repn.constant
                    # We are going to rewrite the core problem file
                    # with all stochastic values set to zero. This will
                    # allow an easy test for missing user annotations.
                    constraint_repn.constant = 0
                    if body_constant is None:
                        body_constant = 0.0
                    symbols = constraint_symbols[con]
                    assert len(symbols) > 0
                    for con_label in symbols:
                        if con_label.startswith('c_e_') or \
                           con_label.startswith('c_l_'):
                            assert (include_bound is True) or \
                                   (include_bound[0] is True)
                            stochastic_rhs_count += 1
                            f_sto.write(rhs_template %
                                        (con_label,
                                         _no_negative_zero(
                                             value(con.lower) - \
                                             value(body_constant))))
                            f_coords.write("RHS %s\n" % (con_label))
                            # We are going to rewrite the core problem file
                            # with all stochastic values set to zero. This will
                            # allow an easy test for missing user annotations.
                            modified_constraint_lb[con] = con.lower
                            con._lower = _deterministic_check_value
                            if con_label.startswith('c_e_'):
                                modified_constraint_ub[con] = con.upper
                                con._upper = _deterministic_check_value
                        elif con_label.startswith('r_l_') :
                            if (include_bound is True) or \
                               (include_bound[0] is True):
                                stochastic_rhs_count += 1
                                f_sto.write(rhs_template %
                                            (con_label,
                                             _no_negative_zero(
                                                 value(con.lower) - \
                                                 value(body_constant))))
                                f_coords.write("RHS %s\n" % (con_label))
                                # We are going to rewrite the core problem file
                                # with all stochastic values set to zero. This will
                                # allow an easy test for missing user annotations.
                                modified_constraint_lb[con] = con.lower
                                con._lower = _deterministic_check_value
                        elif con_label.startswith('c_u_'):
                            assert (include_bound is True) or \
                                   (include_bound[1] is True)
                            stochastic_rhs_count += 1
                            f_sto.write(rhs_template %
                                        (con_label,
                                         _no_negative_zero(
                                             value(con.upper) - \
                                             value(body_constant))))
                            f_coords.write("RHS %s\n" % (con_label))
                            # We are going to rewrite the core problem file
                            # with all stochastic values set to zero. This will
                            # allow an easy test for missing user annotations.
                            modified_constraint_ub[con] = con.upper
                            con._upper = _deterministic_check_value
                        elif con_label.startswith('r_u_'):
                            if (include_bound is True) or \
                               (include_bound[1] is True):
                                stochastic_rhs_count += 1
                                f_sto.write(rhs_template %
                                            (con_label,
                                             _no_negative_zero(
                                                 value(con.upper) - \
                                                 value(body_constant))))
                                f_coords.write("RHS %s\n" % (con_label))
                                # We are going to rewrite the core problem file
                                # with all stochastic values set to zero. This will
                                # allow an easy test for missing user annotations.
                                modified_constraint_ub[con] = con.upper
                                con._upper = _deterministic_check_value
                        else:
                            assert False

            #
            # Stochastic Matrix
            #
            matrix_template = "    %s    %s    %.17g\n"
            if stochastic_matrix is not None:
                for con, var_list in stochastic_matrix_entries:
                    assert isinstance(con, _ConstraintData)
                    if not empty_matrix_annotation:
                        # verify that this constraint was
                        # flagged by PySP or the user as second-stage
                        if id(con) not in secondstage_constraint_ids:
                            raise RuntimeError(
                                "The constraint %s has been declared "
                                "in the %s annotation but it was not identified as "
                                "a second-stage constraint. To correct this issue, "
                                "either remove the constraint from this annotation "
                                "or manually declare it as second-stage using the "
                                "%s annotation."
                                % (con.cname(True),
                                   StochasticConstraintBodyAnnotation.__name__,
                                   ConstraintStageAnnotation.__name__))
                    constraint_repn = \
                        canonical_repn_cache[id(con.parent_block())][con]
                    if not isinstance(constraint_repn, LinearCanonicalRepn):
                        raise RuntimeError("Only linear constraints are "
                                           "accepted for conversion to SMPS format. "
                                           "Constraint %s is not linear."
                                           % (con.cname(True)))
                    assert len(constraint_repn.variables) > 0
                    if var_list is None:
                        var_list = constraint_repn.variables
                    assert len(var_list) > 0
                    symbols = constraint_symbols[con]
                    # sort the variable list by the column ordering
                    # so that we have deterministic output
                    var_list = list(var_list)
                    var_list.sort(key=lambda _v: column_order[_v])
                    new_coefs = list(constraint_repn.linear)
                    for var in var_list:
                        assert isinstance(var, _VarData)
                        assert not var.fixed
                        var_coef = None
                        for i, (_var, coef) in enumerate(zip(constraint_repn.variables,
                                                            constraint_repn.linear)):
                            if _var is var:
                                var_coef = coef
                                # We are going to rewrite with core problem file
                                # with all stochastic values set to zero. This will
                                # allow an easy test for missing user annotations.
                                new_coefs[i] = _deterministic_check_value
                                break
                        if var_coef is None:
                            raise RuntimeError(
                                "The coefficient for variable %s has "
                                "been marked as stochastic in constraint %s using "
                                "the %s annotation, but the variable does not appear"
                                " in the canonical constraint expression."
                                % (var.cname(True),
                                   con.cname(True),
                                   StochasticConstraintBodyAnnotation.__name__))
                        var_label = symbol_map.byObject[id(var)]
                        for con_label in symbols:
                            stochastic_matrix_count += 1
                            f_sto.write(matrix_template
                                        % (var_label,
                                           con_label,
                                           _no_negative_zero(value(var_coef))))
                            f_coords.write("%s %s\n" % (var_label, con_label))

                    constraint_repn.linear = tuple(new_coefs)


            #
            # Stochastic Objective
            #
            obj_template = "    %s    %s    %.17g\n"
            if stochastic_objective is not None:
                if stochastic_objective.has_declarations():
                    sorted_values = stochastic_objective.expand_entries()
                    assert len(sorted_values) <= 1
                    if len(sorted_values) == 0:
                        raise RuntimeError(
                            "The %s annotation was declared "
                            "with explicit entries but no active Objective "
                            "objects were recovered from those entries."
                            % (StochasticObjectiveAnnotation.__name__))
                    obj, (objective_variables, include_constant) = \
                        sorted_values[0]
                    assert obj is objective_object
                else:
                    objective_variables, include_constant = \
                        stochastic_objective.default

                if not isinstance(objective_repn, LinearCanonicalRepn):
                    raise RuntimeError("Only linear stochastic objectives are "
                                       "accepted for conversion to SMPS format. "
                                       "Objective %s is not linear."
                                       % (objective_object.cname(True)))
                if objective_variables is None:
                    objective_variables = objective_repn.variables
                stochastic_objective_label = symbol_map.byObject[id(objective_object)]
                # sort the variable list by the column ordering
                # so that we have deterministic output
                objective_variables = list(objective_variables)
                objective_variables.sort(key=lambda _v: column_order[_v])
                assert (len(objective_variables) > 0) or include_constant
                new_coefs = list(objective_repn.linear)
                for var in objective_variables:
                    assert isinstance(var, _VarData)
                    var_coef = None
                    for i, (_var, coef) in enumerate(zip(objective_repn.variables,
                                                        objective_repn.linear)):
                        if _var is var:
                            var_coef = coef
                            # We are going to rewrite the core problem file
                            # with all stochastic values set to zero. This will
                            # allow an easy test for missing user annotations.
                            new_coefs[i] = _deterministic_check_value
                            break
                    if var_coef is None:
                        raise RuntimeError(
                            "The coefficient for variable %s has "
                            "been marked as stochastic in objective %s using "
                            "the %s annotation, but the variable does not appear"
                            " in the canonical objective expression."
                            % (var.cname(True),
                               objective_object.cname(True),
                               StochasticObjectiveAnnotation.__name__))
                    var_label = symbol_map.byObject[id(var)]
                    stochastic_cost_count += 1
                    f_sto.write(obj_template
                                % (var_label,
                                   stochastic_objective_label,
                                   _no_negative_zero(value(var_coef))))
                    f_coords.write("%s %s\n"
                                   % (var_label,
                                      stochastic_objective_label))

                objective_repn.linear = tuple(new_coefs)
                if include_constant:
                    obj_constant = objective_repn.constant
                    # We are going to rewrite the core problem file
                    # with all stochastic values set to zero. This will
                    # allow an easy test for missing user annotations.
                    objective_repn.constant = 0
                    if obj_constant is None:
                        obj_constant = 0.0
                    stochastic_cost_count += 1
                    f_sto.write(obj_template % ("ONE_VAR_CONSTANT",
                                                stochastic_objective_label,
                                                _no_negative_zero(obj_constant)))
                    f_coords.write("%s %s\n"
                                   % ("ONE_VAR_CONSTANT",
                                      stochastic_objective_label))

    #
    # Write the deterministic part of the LP/MPS-file to its own
    # file for debugging purposes
    #
    reference_model_name = reference_model.name
    reference_model.name = "ZeroStochasticData"
    det_output_filename = \
        os.path.join(output_directory,
                     basename+"."+file_format+".det."+scenario.name)
    with WriterFactory(file_format) as writer:
        output_fname, symbol_map = writer(reference_model,
                                          det_output_filename,
                                          lambda x: True,
                                          io_options)
        assert output_fname == det_output_filename
    reference_model.name = reference_model_name

    # reset bounds on any constraints that were modified
    for con, lower in iteritems(modified_constraint_lb):
        con._lower = lower
    for con, upper in iteritems(modified_constraint_ub):
        con._upper = upper

    return (firststage_variable_count,
            secondstage_variable_count,
            firststage_constraint_count,
            secondstage_constraint_count,
            stochastic_cost_count,
            stochastic_rhs_count,
            stochastic_matrix_count)

def convert_external(output_directory,
                     basename,
                     scenario_tree_manager,
                     core_format='mps',
                     enforce_derived_nonanticipativity=False,
                     io_options=None,
                     disable_consistency_checks=False,
                     keep_scenario_files=False,
                     keep_auxiliary_files=False,
                     verbose=False):
    import pyomo.environ
    import pyomo.solvers.plugins.smanager.phpyro

    if io_options is None:
        io_options = {}

    assert os.path.exists(output_directory)

    scenario_tree = scenario_tree_manager.scenario_tree

    if scenario_tree.contains_bundles():
        raise ValueError(
            "SMPS conversion does not yet handle bundles")

    scenario_directory = os.path.join(output_directory,
                                      'scenario_files')

    if not os.path.exists(scenario_directory):
        os.mkdir(scenario_directory)

    counts = scenario_tree_manager.invoke_function(
        "_convert_external_setup",
        thisfile,
        invocation_type=InvocationType.PerScenario,
        function_args=(scenario_directory,
                       basename,
                       core_format,
                       enforce_derived_nonanticipativity,
                       io_options))

    reference_scenario = scenario_tree.scenarios[0]
    reference_scenario_name = reference_scenario.name

    (firststage_variable_count,
     secondstage_variable_count,
     firststage_constraint_count,
     secondstage_constraint_count,
     stochastic_cost_count,
     stochastic_rhs_count,
     stochastic_matrix_count) = counts[reference_scenario_name]

    #
    # Copy the reference scenario's core, row, col, and tim
    # to the output directory. The consistency checks will
    # verify that these files match across scenarios.
    #
    core_filename = os.path.join(output_directory,
                                 basename+".cor")
    _safe_remove_file(core_filename)
    shutil.copy2(os.path.join(scenario_directory,
                             (basename+"."+core_format+"."+
                              reference_scenario_name)),
                core_filename)

    core_row_filename = os.path.join(output_directory,
                                   basename+".row")
    _safe_remove_file(core_row_filename)
    shutil.copy2(os.path.join(scenario_directory,
                              (basename+".row."+
                               reference_scenario_name)),
                 core_row_filename)

    core_col_filename = os.path.join(output_directory,
                                   basename+".col")
    _safe_remove_file(core_col_filename)
    shutil.copy2(os.path.join(scenario_directory,
                              (basename+".col."+
                               reference_scenario_name)),
                 core_col_filename)

    tim_filename = os.path.join(output_directory,
                                basename+".tim")
    _safe_remove_file(tim_filename)
    shutil.copy2(os.path.join(scenario_directory,
                              (basename+".tim."+
                               reference_scenario_name)),
                 tim_filename)

    sto_struct_filename = os.path.join(output_directory,
                                       basename+".sto.struct")
    _safe_remove_file(sto_struct_filename)
    shutil.copy2(os.path.join(scenario_directory,
                              (basename+".sto.struct."+
                               reference_scenario_name)),
                 sto_struct_filename)

    core_det_filename = os.path.join(output_directory,
                                     basename+"."+core_format+".det")
    _safe_remove_file(core_det_filename)
    shutil.copy2(os.path.join(scenario_directory,
                              (basename+"."+core_format+".det."+
                               reference_scenario_name)),
                 core_det_filename)

    #
    # Merge the per-scenario .sto files into one
    #
    sto_filename = os.path.join(output_directory,
                                basename+".sto")
    _safe_remove_file(sto_filename)
    with open(sto_filename, 'w') as fdst:
        fdst.write('STOCH '+basename+'\n')
        fdst.write('BLOCKS DISCRETE REPLACE\n')
        for scenario in scenario_tree.scenarios:
            scenario_sto_filename = \
                os.path.join(scenario_directory,
                             basename+".sto."+scenario.name)
            assert os.path.exists(scenario_sto_filename)
            with open(scenario_sto_filename, 'r') as fsrc:
                shutil.copyfileobj(fsrc, fdst)
        fdst.write('ENDATA\n')

    if verbose:
        print("\nSMPS Conversion Complete")
        print("Output Saved To: "+os.path.relpath(output_directory))
        print("Basic Problem Information:")
        print(" - Variables:")
        print("   - First Stage: %d"
              % (firststage_variable_count))
        print("   - Second Stage: %d"
              % (secondstage_variable_count))
        print(" - Constraints:")
        print("   - First Stage: %d"
              % (firststage_constraint_count))
        print("   - Second Stage: %d"
              % (secondstage_constraint_count))
        print("   - Stoch. RHS Entries: %d"
              % (stochastic_rhs_count))
        print("   - Stoch. Matrix Entries: %d"
              % (stochastic_matrix_count))
        print(" - Objective:")
        print("    - Stoch. Cost Entries: %d"
              % (stochastic_cost_count))

    if not disable_consistency_checks:
        if verbose:
            print("\nStarting scenario structure consistency checks "
                  "across scenario files stored in %s."
                  % (scenario_directory))
            print("This may take some time. If this test is "
                  "prohibitively slow or can not be executed on "
                  "your system, disable it by activating the "
                  "disable_consistency_check option.")
        has_diff = False
        try:
            if not os.system('diff --help > /dev/null'):
                has_diff = True
            else:
                has_diff = False
        except:
            has_diff = False
        if verbose:
            print(" - Checking row and column ordering...")
        for scenario in scenario_tree.scenarios:
            scenario_core_row_filename = \
                os.path.join(scenario_directory,
                             basename+".row."+scenario.name)
            if has_diff:
                rc = os.system('diff -q '+scenario_core_row_filename+' '+
                               core_row_filename)
            else:
                rc = not filecmp.cmp(scenario_core_row_filename,
                                     core_row_filename,
                                     shallow=False)
            if rc:
                raise ValueError(
                    "The row ordering indicated in file '%s' does not match "
                    "that for scenario %s indicated in file '%s'. This "
                    "suggests that the same constraint is being classified "
                    "in different time stages across scenarios. Consider "
                    "manually declaring constraint stages using the %s "
                    "annotation if not already doing so, or report this "
                    "issue to the PySP developers."
                    % (core_row_filename,
                       scenario.name,
                       scenario_core_row_filename,
                       ConstraintStageAnnotation.__name__))

            scenario_core_col_filename = \
                os.path.join(scenario_directory,
                             basename+".col."+scenario.name)
            if has_diff:
                rc = os.system('diff -q '+scenario_core_col_filename+' '+
                               core_col_filename)
            else:
                rc = not filecmp.cmp(scenario_core_col_filename,
                                     core_col_filename,
                                     shallow=False)
            if rc:
                raise ValueError(
                    "The column ordering indicated in file '%s' does not "
                    "match that for scenario %s indicated in file '%s'. "
                    "This suggests that the set of variables on the model "
                    "changes across scenarios. This is not allowed by the "
                    "SMPS format. If you feel this is a developer error, "
                    "please report this issue to the PySP developers."
                    % (core_col_filename,
                       scenario.name,
                       scenario_core_col_filename))

        if verbose:
            print(" - Checking time-stage classifications...")
        for scenario in scenario_tree.scenarios:
            scenario_tim_filename = \
                os.path.join(scenario_directory,
                             basename+".tim."+scenario.name)
            if has_diff:
                rc = os.system('diff -q '+scenario_tim_filename+' '+
                               tim_filename)
            else:
                rc = not filecmp.cmp(scenario_tim_filename,
                                     tim_filename,
                                     shallow=False)
            if rc:
                raise ValueError(
                    "Main .tim file '%s' does not match .tim file for "
                    "scenario %s located at '%s'. This indicates there was "
                    "a problem translating the reference model to SMPS "
                    "format. Please make sure the problem structure is "
                    "identical over all scenarios (e.g., no. of variables, "
                    "no. of constraints), or report this issue to the PySP "
                    "developers if you feel that it is a developer error."
                    % (tim_filename,
                       scenario.name,
                       scenario_tim_filename))

        if verbose:
            print(" - Checking sparse locations of stochastic elements...")
        for scenario in scenario_tree.scenarios:
            scenario_sto_struct_filename = \
                os.path.join(scenario_directory,
                             basename+".sto.struct."+scenario.name)
            if has_diff:
                rc = os.system('diff -q '+scenario_sto_struct_filename+' '+
                               sto_struct_filename)
            else:
                rc = not filecmp.cmp(scenario_sto_struct_filename,
                                     sto_struct_filename,
                                     shallow=False)
            if rc:
                raise ValueError(
                    "The structure of stochastic entries indicated in file "
                    "'%s' does not match that for scenario %s indicated in "
                    "file '%s'. This suggests that the set of variables "
                    "appearing in some expression declared as stochastic is "
                    "changing across scenarios. If you feel this is a "
                    "developer error, please report this issue to the PySP "
                    "developers." % (sto_struct_filename,
                                     scenario.name,
                                     scenario_sto_struct_filename))

        if verbose:
            print(" - Checking deterministic sections in the core "
                  "problem file...")
        for scenario in scenario_tree.scenarios:
            scenario_core_det_filename = \
                os.path.join(scenario_directory,
                             basename+"."+core_format+".det."+scenario.name)
            if has_diff:
                rc = os.system('diff -q '+scenario_core_det_filename+' '+
                               core_det_filename)
            else:
                rc = not filecmp.cmp(scenario_core_det_filename,
                                     core_det_filename,
                                     shallow=False)
            if rc:
                raise ValueError(
                    "One or more deterministic parts of the problem found "
                    "in file '%s' do not match those for scenario %s found "
                    "in file %s. This suggests that one or more locations "
                    "of stochastic data have not been been annotated on the "
                    "reference Pyomo model. If this seems like a tolerance "
                    "issue or a developer error, please report this issue "
                    "to the PySP developers."
                    % (core_det_filename,
                       scenario.name,
                       scenario_core_det_filename))

    if not keep_auxiliary_files:
        _safe_remove_file(core_row_filename)
        _safe_remove_file(core_col_filename)
        _safe_remove_file(sto_struct_filename)
        _safe_remove_file(core_det_filename)

    if not keep_scenario_files:
        if verbose:
            print("Cleaning temporary per-scenario files")
        for scenario in scenario_tree.scenarios:

            scenario_core_row_filename = \
                os.path.join(scenario_directory,
                             basename+".row."+scenario.name)
            assert os.path.exists(scenario_core_row_filename)
            _safe_remove_file(scenario_core_row_filename)

            scenario_core_col_filename = \
                os.path.join(scenario_directory,
                             basename+".col."+scenario.name)
            assert os.path.exists(scenario_core_col_filename)
            _safe_remove_file(scenario_core_col_filename)

            scenario_tim_filename = \
                os.path.join(scenario_directory,
                             basename+".tim."+scenario.name)
            assert os.path.exists(scenario_tim_filename)
            _safe_remove_file(scenario_tim_filename)

            scenario_sto_struct_filename = \
                os.path.join(scenario_directory,
                             basename+".sto.struct."+scenario.name)
            assert os.path.exists(scenario_sto_struct_filename)
            _safe_remove_file(scenario_sto_struct_filename)

            scenario_sto_filename = \
                os.path.join(scenario_directory,
                             basename+".sto."+scenario.name)
            assert os.path.exists(scenario_sto_filename)
            _safe_remove_file(scenario_sto_filename)

            scenario_core_det_filename = \
                os.path.join(scenario_directory,
                             basename+"."+core_format+".det."+scenario.name)
            assert os.path.exists(scenario_core_det_filename)
            _safe_remove_file(scenario_core_det_filename)

            scenario_core_setup_filename = \
                os.path.join(scenario_directory,
                             basename+".setup."+core_format+"."+scenario.name)
            assert os.path.exists(scenario_core_setup_filename)
            _safe_remove_file(scenario_core_setup_filename)

            scenario_core_filename = \
                os.path.join(scenario_directory,
                             basename+"."+core_format+"."+scenario.name)
            assert os.path.exists(scenario_core_filename)
            _safe_remove_file(scenario_core_filename)

        # only delete this directory if it is empty,
        # it might have previously existed and contains
        # user files
        if len(os.listdir(scenario_directory)) == 0:
            shutil.rmtree(scenario_directory, ignore_errors=True)
    else:
        if verbose:
            print("Temporary per-scenario files are retained in "
                  "scenario_files subdirectory")
        pass

    return ProblemStats(firststage_variable_count=firststage_variable_count,
                        secondstage_variable_count=secondstage_variable_count,
                        firststage_constraint_count=firststage_constraint_count,
                        secondstage_constraint_count=secondstage_constraint_count,
                        stochastic_cost_count=stochastic_cost_count,
                        stochastic_rhs_count=stochastic_rhs_count,
                        stochastic_matrix_count=stochastic_matrix_count,
                        scenario_count=len(scenario_tree.scenarios))

def convert_embedded(output_directory,
                     basename,
                     sp,
                     core_format='mps',
                     io_options=None,
                     enforce_derived_nonanticipativity=False):

    if io_options is None:
        io_options = {}

    import pyomo.environ
    assert os.path.exists(output_directory)
    assert core_format in ('lp', 'mps')

    io_options = dict(io_options)

    if sp.has_stochastic_variable_bounds:
        raise ValueError("Problems with stochastic variables bounds "
                         "can not be converted into an embedded "
                         "SMPS representation")

    #
    # Reinterpret the stage-ness of variables on the sp by
    # pushing derived first-stage variables into the second
    # stage, or keeping them as first-stage variables with
    # non-anticipativity enforced. There is no concept of
    # derived variables in SMPS output.
    #
    first_stage_variables = []
    first_stage_variable_ids = set()
    second_stage_variables = []
    second_stage_variable_ids = set()
    assert len(sp.time_stages) == 2
    assert sorted(sp.time_stages) == sorted(sp.stage_to_variables_map)
    firststage = sp.time_stages[0]
    secondstage = sp.time_stages[1]
    for var, derived in sp.stage_to_variables_map[firststage]:
        if (not derived) or enforce_derived_nonanticipativity:
            first_stage_variables.append(var)
            first_stage_variable_ids.add(id(var))
        else:
            second_stage_variables.append(var)
            second_stage_variable_ids.add(id(var))
    for var, derived in sp.stage_to_variables_map[secondstage]:
        second_stage_variables.append(var)
        second_stage_variable_ids.add(id(var))
    # sort things to keep file output deterministic
    cname_buffer = {}
    first_stage_variables.sort(key=lambda x: x.cname(True, cname_buffer))
    cname_buffer = {}
    second_stage_variables.sort(key=lambda x: x.cname(True, cname_buffer))

    assert len(first_stage_variables) == \
        len(first_stage_variable_ids)
    assert len(second_stage_variables) == \
        len(second_stage_variable_ids)

    #
    # Interpret the stage-ness of constraints based on the
    # appearence of second-stage variables or stochastic data
    # (note that derived first-stage variables are considered
    #  second-stage variables)
    #
    first_stage_constraints = []
    first_stage_constraint_ids = set()
    second_stage_constraints = []
    second_stage_constraint_ids = set()
    for con in sp.reference_model.component_data_objects(
            Constraint,
            active=True,
            descend_into=True):
        constage = sp.compute_constraint_stage(
            con,
            derived_last_stage=not enforce_derived_nonanticipativity)
        if constage == firststage:
            first_stage_constraints.append(con)
            first_stage_constraint_ids.add(id(con))
        else:
            assert constage == secondstage
            second_stage_constraints.append(con)
            second_stage_constraint_ids.add(id(con))
    # sort things to keep file output deterministic
    cname_buffer = {}
    first_stage_constraints.sort(key=lambda x: x.cname(True, cname_buffer))
    cname_buffer = {}
    second_stage_constraints.sort(key=lambda x: x.cname(True, cname_buffer))

    #
    # Create column (variable) ordering maps for LP/MPS files
    #
    column_order = ComponentMap()
    # first stage
    for column_cntr, var in enumerate(first_stage_variables):
        column_order[var] = column_cntr
    # second stage
    for column_cntr, var in enumerate(second_stage_variables,
                                          len(column_order)):
        column_order[var] = column_cntr

    #
    # Create row (constraint) ordering maps for LP/MPS files
    #
    row_order = ComponentMap()
    # first stage
    for row_cntr, var in enumerate(first_stage_constraints):
        row_order[var] = row_cntr
    # second stage
    for row_cntr, var in enumerate(second_stage_constraints,
                                       len(row_order)):
        row_order[var] = row_cntr


    # For consistancy set all stochastic parameters to zero
    # before writing the core file (some may not have been
    # initialized with a value)
    param_vals_orig = ComponentMap()
    for paramdata in sp.stochastic_data:
        param_vals_orig[paramdata] = paramdata.value
        paramdata.value = 0

    #
    # Write the ordered LP/MPS file
    #
    output_filename = os.path.join(output_directory,
                                   basename+".cor")
    with WriterFactory(core_format) as writer:
        assert 'column_order' not in io_options
        assert 'row_order' not in io_options
        io_options['column_order'] = column_order
        io_options['row_order'] = row_order
        io_options['force_objective_constant'] = True
        output_fname, symbol_map = writer(sp.reference_model,
                                          output_filename,
                                          lambda x: True,
                                          io_options)
        assert output_fname == output_filename
    canonical_repn_cache = {}
    for block in sp.reference_model.block_data_objects(
            active=True,
            descend_into=True):
        canonical_repn_cache[id(block)] = block._canonical_repn

    # Reset stochastic parameter to their
    # original setting values
    param_vals_orig = ComponentMap()
    for paramdata, orig_val in param_vals_orig.items():
        paramdata.value = orig_val
    del param_vals_orig

    # Collect constraint symbols and deal with the fact that
    # the LP/MPS writer prepends constraint names with
    # things like 'c_e_', 'c_l_', etc depending on the
    # constraint bound type and will even split a constraint
    # into two constraints if it has two bounds
    constraint_symbols = ComponentMap()
    _reverse_alias = \
        dict((symbol, []) for symbol in symbol_map.bySymbol)
    for alias, obj_weakref in iteritems(symbol_map.aliases):
        _reverse_alias[symbol_map.byObject[id(obj_weakref())]].append(alias)
    # ** SORT POINT TO AVOID NON-DETERMINISTIC ROW ORDERING ***
    for _aliases in itervalues(_reverse_alias):
        _aliases.sort()
    for con in itertools.chain(first_stage_constraints,
                                   second_stage_constraints):
        symbol = symbol_map.byObject[id(con)]
        # if it is a range constraint this will account for
        # that fact and hold and alias for each bound
        aliases = _reverse_alias[symbol]
        constraint_symbols[con] = aliases

    """
    #
    # Write the explicit column ordering (variables) used
    # for the ordered LP/MPS file
    #
    with open(os.path.join(output_directory, basename+".col"), "w") as f_col:
        # first-stage variables
        for var in first_stage_variables:
            varid = id(var)
            if varid in symbol_map.byObject:
                f_col.write(symbol_map.byObject[varid]+"\n")
        # second-stage variables
        for var in second_stage_variables:
            varid = id(var)
            if varid in symbol_map.byObject:
                f_col.write(symbol_map.byObject[varid]+"\n")
        # the writer will always add the dummy variable
        # ONE_VAR_CONSTANT because we force to appear via
        # objective constant (even if it is zero) using the
        # 'force_objective_constant' io option
        f_col.write("ONE_VAR_CONSTANT\n")

    #
    # Write the explicit row ordering (constraints) used
    # for the ordered LP/MPS file
    #
    with open(os.path.join(output_directory, basename+".row"), "w") as f_row:
        # the objective is always the first row in SMPS format
        f_row.write(symbol_map.byObject[id(sp.objective)]+"\n")
        # first-stage constraints
        for con in first_stage_constraints:
            symbols = constraint_symbols[con]
            # because range constraints are split into two
            # constraints (hopefully our ordering of the r_l_
            # and r_u_ forms is the same as the LP/MPS file!)
            for symbol in symbols:
                f_row.write(symbol+"\n")
        # second-stage constraints
        for con in second_stage_constraints:
            symbols = constraint_symbols[con]
            # because range constraints are split into two
            # constraints (hopefully our ordering of the r_l_
            # and r_u_ forms is the same as the LP/MPS file!)
            for symbol in symbols:
                f_row.write(symbol+"\n")
        # the writer will always add the dummy variable
        # ONE_VAR_CONSTANT because we force to appear via
        # objective constant (even if it is zero) using the
        # 'force_objective_constant' io option
        f_row.write("c_e_ONE_VAR_CONSTANT")
    """

    #
    # Write the .tim file
    #
    with open(os.path.join(output_directory, basename+".tim"), "w") as f_tim:
        f_tim.write("TIME %s\n" % (basename))
        if core_format == 'mps':
            f_tim.write("PERIODS IMPLICIT\n")
            f_tim.write("    %s %s TIME1\n"
                        % (symbol_map.byObject[id(first_stage_variables[0])],
                           symbol_map.byObject[id(sp.objective)]))
            symbols = constraint_symbols[second_stage_constraints[0]]
            if len(symbols) == 1:
                # equality constraint
                assert (symbols[0].startswith('c_e_') or \
                        symbols[0].startswith('c_l_') or \
                        symbols[0].startswith('c_u_'))
                stage2_row_start = symbols[0]
            else:
                # range constraint (assumed the LP/MPS writer outputs
                # the lower range constraint first)
                symbols = sorted(symbols)
                assert (symbols[0].startswith('r_l_') or \
                        symbols[0].startswith('r_u_'))
                stage2_row_start = symbols[0]
            # don't assume there is always a second stage variable
            if id(second_stage_variables[0]) in symbol_map.byObject:
                f_tim.write("    %s "
                            % (symbol_map.byObject[id(second_stage_variables[0])]))
            else:
                f_tim.write("    ONE_VAR_CONSTANT ")
            f_tim.write("%s TIME2\n" % (stage2_row_start))
        else:
            assert core_format == "lp"
            f_tim.write("PERIODS EXPLICIT\n")
            f_tim.write("    TIME1\n")
            f_tim.write("    TIME2\n")
            line_template = "    %s %s\n"
            f_tim.write("ROWS\n")
            # the objective is always the first row in SMPS format
            f_tim.write(line_template
                        % (symbol_map.byObject[id(sp.objective)],
                           "TIME1"))
            # first-stage constraints
            for con in first_stage_constraints:
                for symbol in constraint_symbols[con]:
                    f_tim.write(line_template % (symbol, "TIME1"))
            # second-stage constraints
            for con in second_stage_constraints:
                for symbol in constraint_symbols[con]:
                    f_tim.write(line_template % (symbol, "TIME2"))
            f_tim.write(line_template % ("c_e_ONE_VAR_CONSTANT", "TIME2"))

            f_tim.write("COLS\n")
            # first-stage variables
            for var in first_stage_variables:
                varid = id(var)
                if varid in symbol_map.byObject:
                    f_tim.write(line_template % (symbol_map.byObject[varid], "TIME1"))
            # second-stage variables
            for var in second_stage_variables:
                varid = id(var)
                if varid in symbol_map.byObject:
                    f_tim.write(line_template % (symbol_map.byObject[varid], "TIME2"))
            f_tim.write(line_template % ("ONE_VAR_CONSTANT", "TIME2"))

        f_tim.write("ENDATA\n")

    #
    # Write the body of the .sto file
    #
    # **NOTE: In the code that follows we assume the LP/MPS
    #         writer always moves constraint body
    #         constants to the rhs and that the lower part
    #         of any range constraints are written before
    #         the upper part.
    #
    stochastic_data_seen = ComponentMap()
    line_template = "    %s    %s    %.17g    %.17g\n"
    with open(os.path.join(output_directory,
                           basename+".sto"),'w') as f_sto:
        f_sto.write('STOCH '+basename+'\n')
        # TODO: For the time being, we are assuming all
        #       parameter distributions are discrete
        #       tables. This header will need to change when
        #       we start supporting other distributions
        f_sto.write('INDEP         DISCRETE\n')
        constraint_name_buffer = {}
        objective_name_buffer = {}
        variable_name_buffer = {}

        #
        # Stochastic objective elements
        #

        if len(sp.objective_to_stochastic_data_map) > 0:
            assert len(sp.objective_to_stochastic_data_map) == 1
            assert list(sp.objective_to_stochastic_data_map.keys())[0] is sp.objective
            # setting compute values to False allows us to
            # extract the location of Param objects in the
            # constant or variable coefficient
            objective_repn = generate_canonical_repn(sp.objective.expr,
                                                     compute_values=False)
            if not isinstance(objective_repn, LinearCanonicalRepn):
                raise ValueError(
                    "Cannot output embedded SP representation for component "
                    "'%s'. The embedded SMPS writer does not yet handle "
                    "stochastic constraints within nonlinear expressions."
                    % (con.cname(True)))

            # sort the variable list by the column ordering
            # so that we have deterministic output
            objective_vars = list(zip(objective_repn.variables,
                                               objective_repn.linear))
            objective_vars.sort(key=lambda x: column_order[x[0]])
            if objective_repn.constant is not None:
                objective_vars.append(("ONE_VAR_CONSTANT",
                                                objective_repn.constant))
            stochastic_objective_label = symbol_map.byObject[id(sp.objective)]
            for var, varcoef in objective_vars:
                params = list(sp._collect_mutable_parameters(varcoef).values())
                stochastic_params = [p for p in params
                                     if p in sp.stochastic_data]
                # NOTE: Be sure to keep track of
                for param in stochastic_params:
                    if param in stochastic_data_seen:
                        raise ValueError(
                            "Cannot output embedded SP representation for component "
                            "'%s'. The embedded SMPS writer does not yet handle the "
                            "case where a stochastic data component appears in "
                            "multiple expressions or locations within a single "
                            "expression (e.g., multiple constraints, or multiple "
                            "variable coefficients within a constraint). The "
                            "parameter '%s' appearing in component '%s' was "
                            "previously encountered in another location in "
                            "component %s."
                            % (sp.objective.cname(True),
                               param.cname(True),
                               sp.objective.cname(True),
                               stochastic_data_seen[param].cname(True)))
                    else:
                        stochastic_data_seen[param] = sp.objective

                if len(stochastic_params) == 1:
                    paramdata = stochastic_params[0]
                    if varcoef is not paramdata:
                        # TODO: Basically need to rescale / shift
                        # the original distribution. I think both of these
                        # operations are trivial to transform any probability
                        # measure. I'm just not going to get into that right now.
                        raise ValueError(
                            "Cannot output embedded SP representation for component "
                            "'%s'. The embedded SMPS writer does not yet handle the "
                            "case where a stochastic data component appears "
                            "in an expression that defines a single variable's "
                            "coefficient. The coefficient for variable '%s' must be "
                            "exactly set to parameters '%s' in the expression."
                            % (sp.objective.cname(True),
                               (var.cname(True) if var != "ONE_VAR_CONSTANT" else "ONE_VAR_CONSTANT"),
                               paramdata.cname(True)))

                    # output the parameter's distribution (provided by the user)
                    distribution = sp.stochastic_data[paramdata]
                    if type(distribution) is not list:
                        # TODO: relax this when we start supporting other distributions
                        #       or add some object oriented components for defining
                        #       them.
                        raise TypeError(
                            "Invalid distribution type '%s' for stochastic "
                            "parameter '%s'. The embedded SMPS writer currently "
                            "only supports discrete table distributions defined "
                            "by a list of values or a list of (probability, value) "
                            "tuples.")
                    assert len(distribution) > 0
                    if type(distribution[0]) is not tuple:
                        uniformp = 1.0/len(distribution)
                        distribution = [(uniformp, v_) for v_ in distribution]
                    else:
                        assert len(distribution[0]) == 2
                    if not isinstance(var, _VarData):
                        assert var == "ONE_VAR_CONSTANT"
                        varlabel = var
                    else:
                        varlabel = symbol_map.byObject[id(var)]
                    for prob, val in distribution:
                        f_sto.write(line_template % (varlabel,
                                                     stochastic_objective_label,
                                                     _no_negative_zero(val),
                                                     _no_negative_zero(prob)))
                elif len(stochastic_params) > 1:
                    # TODO: Need to output a new distribution based
                    # on some mathematical expression involving
                    # multiple distributions. Might be hard for
                    # general distributions, but would not be that
                    # difficult for discrete tables.
                    raise ValueError(
                        "Cannot output embedded SP representation for component "
                        "'%s'. The embedded SMPS writer does not yet handle the "
                        "case where multiple stochastic data components appear "
                        "in an expression that defines a single variable's "
                        "coefficient. The coefficient for variable '%s' involves "
                        "stochastic parameters: %s"
                        % (sp.objective.cname(True),
                           var.cname(True),
                           [p.cname(True) for p in stochastic_params]))

        #
        # Stochastic constraint matrix and rhs elements
        #

        stochastic_constraints = list(sp.constraint_to_stochastic_data_map.keys())
        stochastic_constraints.sort(key=lambda x: row_order[x])
        for con in stochastic_constraints:

            # setting compute values to False allows us to
            # extract the location of Param objects in the
            # constant or variable coefficient
            constraint_repn = generate_canonical_repn(con.body,
                                                      compute_values=False)
            if not isinstance(constraint_repn, LinearCanonicalRepn):
                raise ValueError(
                    "Cannot output embedded SP representation for component "
                    "'%s'. The embedded SMPS writer does not yet handle "
                    "stochastic constraints within nonlinear expressions."
                    % (con.cname(True)))

            # sort the variable list by the column ordering
            # so that we have deterministic output
            constraint_vars = list(zip(constraint_repn.variables,
                                                constraint_repn.linear))
            constraint_vars.sort(key=lambda x: column_order[x[0]])
            constraint_vars = \
                [(var, symbol_map.byObject[id(var)], varcoef)
                 for var, varcoef in constraint_vars]

            # check if any stochastic data appears in the constant
            # falling out of the body of the constraint expression
            if (constraint_repn.constant is not None):
                # TODO: We can probably support this, just do not want to get
                #       into it right now. It also seems like this is an edge case
                #       that is hard to reproduce because _ConstraintData moves
                #       this stuff out of the body when it is build (so it won't
                #       show up in the body canonical repn)
                for param in sp._collect_mutable_parameters(constraint_repn.constant):
                    if param in sp.stochastic_data:
                        raise ValueError(
                            "Cannot output embedded SP representation for component "
                            "'%s'. The embedded SMPS writer does not yet handle the "
                            "case where a stochastic data appears in the body of a "
                            "constraint expression that must be moved to the bounds. "
                            "The constraint must be written so that the stochastic "
                            "element '%s' is a simple bound or a simple variable "
                            "coefficient." % (con.cname(True),
                                              param.cname(True)))

            symbols = constraint_symbols[con]
            if len(symbols) == 2:
                # TODO: We can handle range constraints (just not in the body).
                #       Will add support for range constraints with stochastic data
                #       in one or both of the bounds later.
                raise ValueError(
                    "Cannot output embedded SP representation for component "
                    "'%s'. The embedded SMPS writer does not yet handle range "
                    "constraints that have stochastic data."
                    % (con.cname(True)))

            # fix this later, for now we assume it is not a range constraint
            assert len(symbols) == 1
            stochastic_constraint_label = symbols[0]
            if stochastic_constraint_label.startswith('c_e_') or \
               stochastic_constraint_label.startswith('c_l_'):
                constraint_vars.append(("RHS","RHS",con.lower))
            elif stochastic_constraint_label.startswith('c_u_'):
                constraint_vars.append(("RHS","RHS",con.upper))

            for var, varlabel, varcoef in constraint_vars:
                params = list(sp._collect_mutable_parameters(varcoef).values())
                stochastic_params = [param for param in params
                                     if param in sp.stochastic_data]
                # NOTE: Be sure to keep track of
                for param in stochastic_params:
                    if param in stochastic_data_seen:
                        raise ValueError(
                            "Cannot output embedded SP representation for component "
                            "'%s'. The embedded SMPS writer does not yet handle the "
                            "case where a stochastic data component appears in "
                            "multiple expressions or locations within a single "
                            "expression (e.g., multiple constraints, or multiple "
                            "variable coefficients within a constraint). The "
                            "parameter '%s' appearing in component '%s' was "
                            "previously encountered in another location in "
                            "component %s."
                            % (con.cname(True),
                               param.cname(True),
                               con.cname(True),
                               stochastic_data_seen[param].cname(True)))
                    else:
                        stochastic_data_seen[param] = con

                if len(stochastic_params) == 1:
                    paramdata = stochastic_params[0]
                    if varcoef is not paramdata:
                        # TODO: Basically need to rescale / shift
                        # the original distribution. I think both of these
                        # operations are trivial to transform any probability
                        # measure. I'm just not going to get into that right now.
                        raise ValueError(
                            "Cannot output embedded SP representation for component "
                            "'%s'. The embedded SMPS writer does not yet handle the "
                            "case where a stochastic data component appears "
                            "in an expression that defines a single variable's "
                            "coefficient. The coefficient for variable '%s' must be "
                            "exactly set to parameters '%s' in the expression."
                            % (con.cname(True),
                               (var.cname(True) if var != "RHS" else "RHS"),
                               paramdata.cname(True)))

                    # output the parameter's distribution (provided by the user)
                    distribution = sp.stochastic_data[paramdata]
                    if type(distribution) is not list:
                        # TODO: relax this when we start supporting other distributions
                        #       or add some object oriented components for defining
                        #       them.
                        raise TypeError(
                            "Invalid distribution type '%s' for stochastic "
                            "parameter '%s'. The embedded SMPS writer currently "
                            "only supports discrete table distributions defined "
                            "by a list of values or a list of (probability, value) "
                            "tuples.")
                    assert len(distribution) > 0
                    if type(distribution[0]) is not tuple:
                        uniformp = 1.0/len(distribution)
                        distribution = [(uniformp, v_) for v_ in distribution]
                    else:
                        assert len(distribution[0]) == 2
                    for prob, val in distribution:
                        f_sto.write(line_template % (varlabel,
                                                     stochastic_constraint_label,
                                                     _no_negative_zero(val),
                                                     _no_negative_zero(prob)))
                elif len(stochastic_params) > 1:
                    # TODO: Need to output a new distribution based
                    # on some mathematical expression involving
                    # multiple distributions. Might be hard for
                    # general distributions, but would not be that
                    # difficult for discrete tables.
                    raise ValueError(
                        "Cannot output embedded SP representation for component "
                        "'%s'. The embedded SMPS writer does not yet handle the "
                        "case where multiple stochastic data components appear "
                        "in an expression that defines a single variable's "
                        "coefficient. The coefficient for variable '%s' involves "
                        "stochastic parameters: %s"
                        % (con.cname(True),
                           var.cname(True),
                           [param.cname(True) for param in stochastic_params]))
        f_sto.write("ENDATA\n")

    return symbol_map
