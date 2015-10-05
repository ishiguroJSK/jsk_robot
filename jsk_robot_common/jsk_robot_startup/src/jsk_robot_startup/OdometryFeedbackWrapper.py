#! /usr/bin/env python

import rospy
import numpy
from nav_msgs.msg import Odometry
from std_msgs.msg import Float64
from geometry_msgs.msg import Quaternion, Twist, Vector3
import tf
import sys
import threading
import copy
from scipy import signal
from dynamic_reconfigure.server import Server
from jsk_robot_startup.cfg import OdometryFeedbackWrapperReconfigureConfig

class OdometryFeedbackWrapper(object):
    def __init__(self):
        rospy.init_node("OdometryFeedbackWrapper", anonymous=True)
        self.rate = float(rospy.get_param("~rate", 100))
        self.publish_tf = rospy.get_param("~publish_tf", True)
        self.invert_tf = rospy.get_param("~invert_tf", True)
        self.broadcast = tf.TransformBroadcaster()
        self.listener = tf.TransformListener()
        self.odom_frame = rospy.get_param("~odom_frame", "feedback_odom")
        self.base_link_frame = rospy.get_param("~base_link_frame", "BODY")
        self.odom = None # belief of this wrapper
        self.feedback_odom = None
        self.source_odom = None
        self.dt = 0.0
        self.prev_time = rospy.Time.now()
        self.r = rospy.Rate(self.rate)
        self.lock = threading.Lock()
        self.odom_history = []
        self.v_sigma = [rospy.get_param("~sigma_x", 0.05),
                        rospy.get_param("~sigma_y", 0.1),
                        rospy.get_param("~sigma_z", 0.0001),
                        rospy.get_param("~sigma_roll", 0.0001),
                        rospy.get_param("~sigma_pitch", 0.0001),
                        rospy.get_param("~sigma_yaw", 0.01)]
        self.feedback_enabled_sigma = rospy.get_param("~feedback_enabled_sigma", 0.5)
        self.pub = rospy.Publisher("~output", Odometry, queue_size=10)
        self.init_odom_sub = rospy.Subscriber("~init_odom", Odometry, self.init_odom_callback)
        self.source_odom_sub = rospy.Subscriber("~source_odom", Odometry, self.source_odom_callback)
        self.feedback_odom_sub = rospy.Subscriber("~feedback_odom", Odometry, self.feedback_odom_callback)
        self.reconfigure_server = Server(OdometryFeedbackWrapperReconfigureConfig, self.reconfigure_callback)

    def execute(self):
        while not rospy.is_shutdown():
            self.update()
            self.r.sleep()


    def reconfigure_callback(self, config, level):
        with self.lock:
            for i, sigma in enumerate(["sigma_x", "sigma_y", "sigma_z", "sigma_roll", "sigma_pitch", "sigma_yaw"]):
                self.v_sigma[i] = config[sigma]
        rospy.loginfo("[%s]" + "velocity sigma updated: x: {0}, y: {1}, z: {2}, roll: {3}, pitch: {4}, yaw: {5}".format(*self.v_sigma), rospy.get_name())
        self.feedback_enabled_sigma = config["feedback_enabled_sigma"]
        rospy.loginfo("[%s]: feedback sigma is %f", rospy.get_name(), self.feedback_enabled_sigma)
        return config

    def init_odom_callback(self, msg):
        with self.lock:
            if not self.odom: # initialize buffers
                self.odom = msg
                self.odom.header.frame_id = self.odom_frame
                self.odom.child_frame_id = self.base_link_frame
                self.prev_time = rospy.Time.now()

    def source_odom_callback(self, msg):
        if not self.odom:
            return
        with self.lock:
            self.source_odom = msg

    def feedback_odom_callback(self, msg):
        if not self.odom:
            return
        self.feedback_odom = msg        
        with self.lock:
            # check distribution accuracy
            nearest_odom = copy.copy(self.odom)
            nearest_dt = (self.feedback_odom.header.stamp - self.odom.header.stamp).to_sec()
            for hist in self.odom_history:
                dt = (self.feedback_odom.header.stamp - hist.header.stamp).to_sec()
                if abs(dt) < abs(nearest_dt):
                    nearest_dt = dt
                    nearest_odom = copy.copy(hist)
            self.update_pose(nearest_odom.pose, nearest_odom.twist,
                             nearest_odom.header.frame_id, nearest_odom.child_frame_id,
                             nearest_odom.header.stamp, nearest_dt)
            enable_feedback = self.check_covaraicne(nearest_odom) or self.check_distribution_difference(nearest_odom, self.feedback_odom)
            if enable_feedback:
                rospy.loginfo("%s: Feedback enabled.", rospy.get_name())
                self.feedback_odom = msg
                for hist in self.odom_history:
                    dt = (hist.header.stamp - self.feedback_odom.header.stamp).to_sec()
                    if dt > 0.0:
                        # update pose and twist according to the history
                        self.update_twist(self.feedback_odom.twist, hist.twist)
                        self.update_pose(self.feedback_odom.pose, hist.twist,
                                         self.feedback_odom.header.frame_id, hist.child_frame_id,
                                         hist.header.stamp, dt) # update feedback_odom according to twist of hist
                        # update covariance
                        # this wrapper do not upgrade twist.covariance to trust feedback_odom.covariance
                        self.update_pose_covariance(self.feedback_odom.pose, self.feedback_odom.twist,
                                                    self.feedback_odom.header.frame_id, hist.child_frame_id,       
                                                    hist.header.stamp, dt)
                        self.feedback_odom.header.stamp = hist.header.stamp
                self.odom.pose = self.feedback_odom.pose
                self.prev_time = self.feedback_odom.header.stamp
                self.odom_history = []

    def check_covaraicne(self, odom):
        for cov in odom.pose.covariance:
            if cov > self.feedback_enabled_sigma ** 2:
                rospy.loginfo("%s: Covariance exceeds limitation. %f > %f", rospy.get_name(), cov, self.feedback_enabled_sigma)
                return True
        return False

    def check_distribution_difference(self, nearest_odom, feedback_odom):
        def make_pose_set(odom):
            odom_euler = tf.transformations.euler_from_quaternion((odom.pose.pose.orientation.x, odom.pose.pose.orientation.y,
                                                                   odom.pose.pose.orientation.z, odom.pose.pose.orientation.w))
            odom_pose_list = [odom.pose.pose.position.x, odom.pose.pose.position.y, odom.pose.pose.position.z,
                              odom_euler[0], odom_euler[1], odom_euler[2]]
            odom_cov_matrix = numpy.matrix(odom.pose.covariance).reshape(6, 6)
            return odom_pose_list, odom_cov_matrix
        nearest_odom_pose, nearest_odom_cov_matrix = make_pose_set(nearest_odom)
        feedback_odom_pose, feedback_odom_cov_matrix = make_pose_set(feedback_odom)        
        for i in range(6):
            if abs(nearest_odom_pose[i] - feedback_odom_pose[i]) > numpy.sqrt(nearest_odom_cov_matrix[i, i]):
                rospy.loginfo("%s: Pose difference is larger than original sigma.%f > %f",
                              rospy.get_name(), abs(nearest_odom_pose[i] - feedback_odom_pose[i]), numpy.sqrt(nearest_odom_cov_matrix[i, i]))
                return True
        return False
            
    def update(self):
        if not self.odom or not self.source_odom:
            return
        with self.lock:
            self.dt = (rospy.Time.now() - self.prev_time).to_sec()           
            if self.dt > 0.0:
                # if self.dt > 2 * (1.0 / self.rate):
                #     rospy.logwarn("[%s]Execution time is violated. Target: %f[sec], Current: %f[sec]", rospy.get_name(), 1.0 / self.rate, self.dt)
                self.calc_odometry()
                self.calc_covariance()
                self.publish_odometry()
                self.prev_time = rospy.Time.now()
                if self.publish_tf:
                    self.broadcast_transform()


    def calc_odometry(self):
        self.update_twist(self.odom.twist, self.source_odom.twist)
        self.update_pose(self.odom.pose, self.odom.twist, self.odom.header.frame_id, self.odom.child_frame_id, rospy.Time(0), self.dt)

    def calc_covariance(self):
        self.update_twist_covariance(self.odom.twist)
        self.update_pose_covariance(self.odom.pose, self.odom.twist, self.odom.header.frame_id, self.odom.child_frame_id, rospy.Time(0), self.dt)
        
    def publish_odometry(self):
        self.odom.header.stamp = rospy.Time.now()
        self.pub.publish(self.odom)
        self.odom_history.append(copy.copy(self.odom))

    def update_twist(self, twist, new_twist):
        twist.twist = new_twist.twist

    def update_pose(self, pose, twist, pose_frame, twist_frame, stamp, dt):
        try:
            (trans,rot) = self.listener.lookupTransform(pose_frame, twist_frame, stamp)
        except:
            try:
                rospy.logwarn("timestamp %f of tf (%s to %s) is not correct. use rospy.Time(0).",  stamp.to_sec(), pose_frame, twist_frame)
                (trans,rot) = self.listener.lookupTransform(pose_frame, twist_frame, rospy.Time(0)) # todo: lookup odom.header.stamp
            except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                rospy.logwarn("failed to solve tf: %s to %s", pose_frame, twist_frame)
                return
        rotation_matrix = tf.transformations.quaternion_matrix(rot)[:3, :3]
        global_velocity = numpy.dot(rotation_matrix, numpy.array([[twist.twist.linear.x],
                                                                  [twist.twist.linear.y],
                                                                  [twist.twist.linear.z]]))
        global_omega = numpy.dot(rotation_matrix, numpy.array([[twist.twist.angular.x],
                                                               [twist.twist.angular.y],
                                                               [twist.twist.angular.z]]))
        pose.pose.position.x += global_velocity[0, 0] * dt
        pose.pose.position.y += global_velocity[1, 0] * dt
        pose.pose.position.z += global_velocity[2, 0] * dt
        euler = list(tf.transformations.euler_from_quaternion((pose.pose.orientation.x, pose.pose.orientation.y,
                                                               pose.pose.orientation.z, pose.pose.orientation.w)))
        euler[0] += global_omega[0, 0] * dt
        euler[1] += global_omega[1, 0] * dt
        euler[2] += global_omega[2, 0] * dt
        quat = tf.transformations.quaternion_from_euler(*euler)
        pose.pose.orientation = Quaternion(*quat)
        
    def update_twist_covariance(self, twist):
        # twist_proportional_sigma = [twist.twist.linear.x * self.v_sigma[0], twist.twist.linear.y * self.v_sigma[1], twist.twist.linear.z * self.v_sigma[2],
        #                             twist.twist.angular.x * self.v_sigma[3], twist.twist.angular.y * self.v_sigma[4], twist.twist.angular.z * self.v_sigma[5]]
        # twist.covariance = numpy.diag([max(x**2, 0.001*0.001) for x in twist_proportional_sigma]).reshape(-1,).tolist() # covariance should be singular

        twist_list = [twist.twist.linear.x, twist.twist.linear.y, twist.twist.linear.z, twist.twist.angular.x, twist.twist.angular.y, twist.twist.angular.z]
        current_sigma = []
        for i in range(6):
            if abs(twist_list[i]) < 0.001:
                current_sigma.append(0.001)
            else:
                current_sigma.append(self.v_sigma[i])
        twist.covariance = numpy.diag([max(x**2, 0.001*0.001) for x in current_sigma]).reshape(-1,).tolist() # covariance should be singular


    def update_pose_covariance(self, pose, twist, pose_frame, twist_frame, stamp, dt):
        # make matirx from covarinace array
        prev_pose_cov_matrix = numpy.matrix(pose.covariance).reshape(6, 6)
        twist_cov_matrix = numpy.matrix(twist.covariance).reshape(6, 6)
        # twist is described in child_frame_id coordinates
        try:
            (trans,rot) = self.listener.lookupTransform(pose_frame, twist_frame, stamp)
        except:
            try:
                rospy.logwarn("timestamp %f of tf (%s to %s) is not correct. use rospy.Time(0).",  stamp.to_sec(), pose_frame, twist_frame)
                (trans,rot) = self.listener.lookupTransform(pose_frame, twist_frame, rospy.Time(0)) # todo: lookup odom.header.stamp
            except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                rospy.logwarn("failed to solve tf: %s to %s", pose_frame, twist_frame)
                return
        rotation_matrix = tf.transformations.quaternion_matrix(rot)[:3, :3]
        global_twist_cov_matrix = numpy.zeros((6, 6))
        global_twist_cov_matrix[:3, :3] = (rotation_matrix.T).dot(twist_cov_matrix[:3, :3].dot(rotation_matrix))
        global_twist_cov_matrix[3:6, 3:6] = (rotation_matrix.T).dot(twist_cov_matrix[3:6, 3:6].dot(rotation_matrix))
        # jacobian matrix
        # elements in pose and twist are assumed to be independent on global coordinates
        jacobi_pose = numpy.diag([1.0] * 6)
        jacobi_twist = numpy.diag([dt] * 6)
        # covariance calculation
        pose_cov_matrix = jacobi_pose.dot(prev_pose_cov_matrix.dot(jacobi_pose.T)) + jacobi_twist.dot(global_twist_cov_matrix.dot(jacobi_twist.T))
        # update covariances as array type (twist is same as before)
        pose.covariance = numpy.array(pose_cov_matrix).reshape(-1,).tolist()
      
    def broadcast_transform(self):
        if not self.odom:
            return
        position = [self.odom.pose.pose.position.x, self.odom.pose.pose.position.y, self.odom.pose.pose.position.z]
        orientation = [self.odom.pose.pose.orientation.x, self.odom.pose.pose.orientation.y, self.odom.pose.pose.orientation.z, self.odom.pose.pose.orientation.w]
        if self.invert_tf:
            homogeneous_matrix = tf.transformations.quaternion_matrix(orientation)
            homogeneous_matrix[:3, 3] = numpy.array(position).reshape(1, 3)
            homogeneous_matrix_inv = numpy.linalg.inv(homogeneous_matrix)
            position = list(homogeneous_matrix_inv[:3, 3])
            orientation = list(tf.transformations.quaternion_from_matrix(homogeneous_matrix_inv))
            parent_frame = self.odom.child_frame_id
            target_frame = self.odom.header.frame_id
        else:
            parent_frame = self.odom.header.frame_id
            target_frame = self.odom.child_frame_id
        self.broadcast.sendTransform(position, orientation, rospy.Time.now(), target_frame, parent_frame)
