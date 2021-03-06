import functools

import py_trees
import py_trees_ros
import rospy
from control_msgs.msg import JointTrajectoryControllerState
from giskard_msgs.msg import MoveAction
from py_trees import Sequence, Selector, BehaviourTree, Blackboard
from py_trees.meta import failure_is_success, success_is_failure, success_is_running
from py_trees_ros.trees import BehaviourTree
from rospy import ROSException

import giskardpy.identifier as identifier
import giskardpy.pybullet_wrapper as pbw
from giskardpy import logging
from giskardpy.god_map import GodMap
from giskardpy.input_system import JointStatesInput
from giskardpy.plugin import PluginBehavior
from giskardpy.plugin_action_server import GoalReceived, SendResult, GoalCanceled
from giskardpy.plugin_attached_tf_publicher import TFPlugin
from giskardpy.plugin_cleanup import CleanUp
from giskardpy.plugin_collision_checker import CollisionChecker
from giskardpy.plugin_configuration import ConfigurationPlugin
from giskardpy.plugin_cpi_marker import CPIMarker
from giskardpy.plugin_goal_reached import GoalReachedPlugin
from giskardpy.plugin_if import IF
from giskardpy.plugin_instantaneous_controller import ControllerPlugin
from giskardpy.plugin_interrupts import WiggleCancel
from giskardpy.plugin_kinematic_sim import KinSimPlugin
from giskardpy.plugin_log_trajectory import LogTrajPlugin
from giskardpy.plugin_plot_trajectory import PlotTrajectory
from giskardpy.plugin_pybullet import WorldUpdatePlugin
from giskardpy.plugin_send_trajectory import SendTrajectory
from giskardpy.plugin_set_cmd import SetCmd
from giskardpy.plugin_time import TimePlugin
from giskardpy.plugin_update_constraints import GoalToConstraints
from giskardpy.plugin_visualization import VisualizationBehavior
from giskardpy.pybullet_world import PyBulletWorld
from giskardpy.utils import create_path, render_dot_tree, KeyDefaultDict
from giskardpy.world_object import WorldObject

def initialize_god_map():
    god_map = GodMap()
    blackboard = Blackboard
    blackboard.god_map = god_map
    god_map.safe_set_data(identifier.rosparam, rospy.get_param(rospy.get_name()))
    god_map.safe_set_data(identifier.robot_description, rospy.get_param(u'robot_description'))
    path_to_data_folder = god_map.safe_get_data(identifier.data_folder)
    # fix path to data folder
    if not path_to_data_folder.endswith(u'/'):
        path_to_data_folder += u'/'
    god_map.safe_set_data(identifier.data_folder, path_to_data_folder)

    # fix nWSR
    nWSR = god_map.safe_get_data(identifier.nWSR)
    if nWSR == u'None':
        nWSR = None
    god_map.safe_set_data(identifier.nWSR, nWSR)

    pbw.start_pybullet(god_map.safe_get_data(identifier.gui))
    while not rospy.is_shutdown():
        try:
            controlled_joints = rospy.wait_for_message(u'/whole_body_controller/state',
                                                       JointTrajectoryControllerState,
                                                       timeout=5.0).joint_names
        except ROSException as e:
            logging.logerr(u'state topic not available')
            logging.logerr(e)
        else:
            break
        rospy.sleep(0.5)



    joint_weight_symbols = process_joint_specific_params(identifier.joint_cost,
                                                         identifier.default_joint_cost_identifier, god_map)

    process_joint_specific_params(identifier.distance_thresholds, identifier.default_collision_distances, god_map)

    joint_vel_symbols = process_joint_specific_params(identifier.joint_vel, identifier.default_joint_vel, god_map)

    joint_acc_symbols = process_joint_specific_params(identifier.joint_acc, identifier.default_joint_acc, god_map)

    world = PyBulletWorld(god_map.safe_get_data(identifier.gui),
                          blackboard.god_map.safe_get_data(identifier.data_folder))
    god_map.safe_set_data(identifier.world, world)
    robot = WorldObject(god_map.safe_get_data(identifier.robot_description),
                        None,
                        controlled_joints)
    world.add_robot(robot, None, controlled_joints, joint_vel_symbols, joint_acc_symbols, joint_weight_symbols, True,
                    ignored_pairs=god_map.safe_get_data(identifier.ignored_self_collisions),
                    added_pairs=god_map.safe_get_data(identifier.added_self_collisions))
    joint_position_symbols = JointStatesInput(blackboard.god_map.to_symbol, world.robot.get_controllable_joints(),
                                identifier.joint_states,
                                suffix=[u'position'])
    joint_vel_symbols = JointStatesInput(blackboard.god_map.to_symbol, world.robot.get_controllable_joints(),
                                identifier.joint_states,
                                suffix=[u'velocity'])
    world.robot.reinitialize(joint_position_symbols.joint_map, joint_vel_symbols.joint_map)
    return god_map

def process_joint_specific_params(identifier_, default, god_map):
    d = KeyDefaultDict(lambda key: god_map.get_data(default))
    d.update(god_map.safe_get_data(identifier_))
    god_map.safe_set_data(identifier_, d)
    return KeyDefaultDict(lambda key: god_map.to_symbol(identifier_ + [key]))

def grow_tree():
    action_server_name = u'giskardpy/command'

    god_map = initialize_god_map()
    # ----------------------------------------------
    wait_for_goal = Sequence(u'wait for goal')
    wait_for_goal.add_child(TFPlugin(u'tf'))
    wait_for_goal.add_child(ConfigurationPlugin(u'js'))
    wait_for_goal.add_child(WorldUpdatePlugin(u'pybullet updater'))
    wait_for_goal.add_child(GoalReceived(u'has goal', action_server_name, MoveAction))
    wait_for_goal.add_child(ConfigurationPlugin(u'js'))
    # ----------------------------------------------
    planning_3 = PluginBehavior(u'planning III', sleep=0)
    planning_3.add_plugin(CollisionChecker(u'coll'))
    # if god_map.safe_get_data(identifier.enable_collision_marker):
    #     planning_3.add_plugin(success_is_running(CPIMarker)(u'cpi marker'))
    planning_3.add_plugin(ControllerPlugin(u'controller'))
    planning_3.add_plugin(KinSimPlugin(u'kin sim'))
    planning_3.add_plugin(LogTrajPlugin(u'log'))
    planning_3.add_plugin(GoalReachedPlugin(u'goal reached'))
    planning_3.add_plugin(WiggleCancel(u'wiggle'))
    planning_3.add_plugin(TimePlugin(u'time'))
    # ----------------------------------------------
    publish_result = failure_is_success(Selector)(u'monitor execution')
    publish_result.add_child(GoalCanceled(u'goal canceled', action_server_name))
    publish_result.add_child(SendTrajectory(u'send traj'))
    # ----------------------------------------------
    # ----------------------------------------------
    planning_2 = failure_is_success(Selector)(u'planning II')
    planning_2.add_child(GoalCanceled(u'goal canceled', action_server_name))
    # planning.add_child(CollisionCancel(u'in collision', collision_time_threshold))
    if god_map.safe_get_data(identifier.enable_VisualizationBehavior):
        planning_2.add_child(success_is_failure(VisualizationBehavior)(u'visualization'))
    if god_map.safe_get_data(identifier.enable_CPIMarker):
        planning_2.add_child(success_is_failure(CPIMarker)(u'cpi marker'))
    planning_2.add_child(planning_3)
    # ----------------------------------------------
    move_robot = failure_is_success(Sequence)(u'move robot')
    move_robot.add_child(IF(u'execute?', identifier.execute))
    move_robot.add_child(publish_result)
    # ----------------------------------------------
    # ----------------------------------------------
    planning_1 = success_is_failure(Sequence)(u'planning I')
    planning_1.add_child(GoalToConstraints(u'update constraints', action_server_name))
    planning_1.add_child(planning_2)
    # ----------------------------------------------
    # ----------------------------------------------
    process_move_goal = failure_is_success(Selector)(u'process move goal')
    process_move_goal.add_child(planning_1)
    process_move_goal.add_child(SetCmd(u'set move goal', action_server_name))
    # ----------------------------------------------
    # ----------------------------------------------
    root = Sequence(u'root')
    root.add_child(wait_for_goal)
    root.add_child(CleanUp(u'cleanup'))
    root.add_child(process_move_goal)
    if god_map.safe_get_data(identifier.enable_PlotTrajectory):
        root.add_child(PlotTrajectory(u'plot trajectory'))
    root.add_child(move_robot)
    root.add_child(SendResult(u'send result', action_server_name, MoveAction))

    tree = BehaviourTree(root)

    if god_map.safe_get_data(identifier.debug):
        def post_tick(snapshot_visitor, behaviour_tree):
            logging.logdebug(u'\n' + py_trees.display.ascii_tree(behaviour_tree.root,
                                                                 snapshot_information=snapshot_visitor))

        snapshot_visitor = py_trees_ros.visitors.SnapshotVisitor()
        tree.add_post_tick_handler(functools.partial(post_tick, snapshot_visitor))
        tree.visitors.append(snapshot_visitor)
    path = god_map.safe_get_data(identifier.data_folder) + u'tree'
    create_path(path)
    render_dot_tree(root, name=path)

    tree.setup(30)
    return tree
