# -*- coding: utf-8 -*-
#
# This file is part of INGInious. See the LICENSE and the COPYRIGHTS files for
# more information about the licensing of this file.

import asyncio
import base64
import logging
import os
import shutil
import struct
import tempfile
from os.path import join as path_join

import msgpack
import psutil
from inginious.agent.docker_agent._docker_interface import DockerInterface

from inginious.agent import Agent, CannotCreateJobException
from inginious.agent.docker_agent._timeout_watcher import TimeoutWatcher
from inginious.common.asyncio_utils import AsyncIteratorWrapper, AsyncProxy
from inginious.common.base import id_checker, id_checker_tests
from inginious.common.filesystems.provider import FileSystemProvider
from inginious.common.messages import BackendNewJob, BackendKillJob


class DockerAgent(Agent):
    def __init__(self, context, backend_addr, friendly_name, concurrency, filesystem, address_host=None, external_ports=None, tmp_dir="./agent_tmp"):
        """
        :param context: ZeroMQ context for this process
        :param backend_addr: address of the backend (for example, "tcp://127.0.0.1:2222")
        :param friendly_name: a string containing a friendly name to identify agent
        :param concurrency: number of simultaneous jobs that can be run by this agent
        :param tasks_fs: FileSystemProvider for the course / tasks
        :param address_host: hostname/ip/... to which external client should connect to access to the docker
        :param external_ports: iterable containing ports to which the docker instance can bind internal ports
        :param tmp_dir: temp dir that is used by the agent to start new containers
        """
        super(DockerAgent, self).__init__(context, backend_addr, friendly_name, concurrency, filesystem)
        self._logger = logging.getLogger("inginious.agent.docker")

        self._max_memory_per_slot = int(psutil.virtual_memory().total / concurrency / 1024 / 1024)

        # Temp dir
        self._tmp_dir = tmp_dir

        # SSH remote debug
        self._address_host = address_host
        self._external_ports = set(external_ports) if external_ports is not None else set()

        # Async proxy to os
        self._aos = AsyncProxy(os)
        self._ashutil = AsyncProxy(shutil)

    async def _init_clean(self):
        """ Must be called when the agent is starting """
        # Data about running containers
        self._containers_running = {}
        self._container_for_job = {}

        self._student_containers_running = {}
        self._student_containers_for_job = {}

        self._containers_killed = dict()

        # Delete tmp_dir, and recreate-it again
        try:
            await self._ashutil.rmtree(self._tmp_dir)
        except OSError:
            pass

        try:
            await self._aos.mkdir(self._tmp_dir)
        except OSError:
            pass

        # Docker
        self._docker = AsyncProxy(DockerInterface())

        # Auto discover containers
        self._logger.info("Discovering containers")
        self._containers = await self._docker.get_containers()
        for idx in self._containers:
            self._containers[idx]["type"] = "docker" # type is not given by self._docker.get_containers()

        self._assigned_external_ports = {}  # container_id : [external_ports]

        if self._address_host is None and len(self._containers) != 0:
            self._logger.info("Guessing external host IP")
            self._address_host = await self._docker.get_host_ip(next(iter(self._containers.values()))["id"])
        if self._address_host is None:
            self._logger.warning(
                "Cannot find external host IP. Please indicate it in the configuration. Remote SSH debug has been deactivated.")
            self._external_ports = None
        else:
            self._logger.info("External address for SSH remote debug is %s", self._address_host)

        # Watchers
        self._timeout_watcher = TimeoutWatcher(self._docker)

    async def _end_clean(self):
        """ Must be called when the agent is closing """
        await self._timeout_watcher.clean()

        async def close_and_delete(container_id):
            try:
                await self._docker.remove_container(container_id)
            except:
                pass

        for container_id  in self._containers_running:
            await close_and_delete(container_id)
        for container_id  in self._student_containers_running:
            await close_and_delete(container_id)

    @property
    def environments(self):
        return self._containers

    async def _watch_docker_events(self):
        """ Get raw docker events and convert them to more readable objects, and then give them to self._docker_events_subscriber """
        try:
            source = AsyncIteratorWrapper(self._docker.sync.event_stream(filters={"event": ["die", "oom"]}))
            async for i in source:
                if i["Type"] == "container" and i["status"] == "die":
                    container_id = i["id"]
                    try:
                        retval = int(i["Actor"]["Attributes"]["exitCode"])
                    except asyncio.CancelledError:
                        raise
                    except:
                        self._logger.exception("Cannot parse exitCode for container %s", container_id)
                        retval = -1

                    if container_id in self._containers_running:
                        self._create_safe_task(self.handle_job_closing(container_id, retval))
                    elif container_id in self._student_containers_running:
                        self._create_safe_task(self.handle_student_job_closing(container_id, retval))
                elif i["Type"] == "container" and i["status"] == "oom":
                    container_id = i["id"]
                    if container_id in self._containers_running or container_id in self._student_containers_running:
                        self._logger.info("Container %s did OOM, killing it", container_id)
                        self._containers_killed[container_id] = "overflow"
                        try:
                            self._create_safe_task(self._docker.kill_container(container_id))
                        except asyncio.CancelledError:
                            raise
                        except:  # this call can sometimes fail, and that is normal.
                            pass
                else:
                    raise TypeError(str(i))
        except asyncio.CancelledError:
            pass
        except:
            self._logger.exception("Exception in _watch_docker_events")

    def __new_job_sync(self, message: BackendNewJob, future_results):
        """ Synchronous part of _new_job. Creates needed directories, copy files, and starts the container. """
        course_id = message.course_id
        task_id = message.task_id

        debug = message.debug
        environment_name = message.environment

        try:
            enable_network = message.environment_parameters.get("network_grading", False)
            limits = message.environment_parameters.get("limits", {})
            time_limit = int(limits.get("time", 30))
            hard_time_limit = int(limits.get("hard_time", None) or time_limit * 3)
            mem_limit = int(limits.get("memory", 200))
            run_cmd = message.environment_parameters.get("run_cmd", '')
        except:
            raise CannotCreateJobException('The agent is unable to parse the parameters')

        course_fs = self._fs.from_subfolder(course_id)
        task_fs = course_fs.from_subfolder(task_id)

        if not course_fs.exists() or not task_fs.exists():
            self._logger.warning("Task %s/%s unavailable on this agent", course_id, task_id)
            raise CannotCreateJobException('Task unavailable on agent. Please retry later, the agents should synchronize soon. '
                             'If the error persists, please contact your course administrator.')

        # Check for realistic memory limit value
        if mem_limit < 20:
            mem_limit = 20
        elif mem_limit > self._max_memory_per_slot:
            self._logger.warning("Task %s/%s ask for too much memory (%dMB)! Available: %dMB", course_id, task_id,
                                 mem_limit, self._max_memory_per_slot)
            raise CannotCreateJobException('Not enough memory on agent (available: %dMB). Please contact your course administrator.' % self._max_memory_per_slot)

        if environment_name not in self._containers:
            self._logger.warning("Task %s/%s ask for an unknown environment %s (not in aliases)", course_id, task_id,
                                 environment_name)
            raise CannotCreateJobException('Unknown container. Please contact your course administrator.')

        environment = self._containers[environment_name]["id"]

        ports_needed = list(self._containers[environment_name]["ports"])  # copy, as we modify it later!

        if debug == "ssh" and 22 not in ports_needed:
            ports_needed.append(22)

        ports = {}
        if len(ports_needed) > 0:
            time_limit = 30 * 60
            hard_time_limit = 30 * 60
        for p in ports_needed:
            if len(self._external_ports) == 0:
                self._logger.warning("User asked for a port but no one are available")
                raise CannotCreateJobException('No ports are available right now. Please retry later.')
            ports[p] = self._external_ports.pop()

        # Create directories for storing all the data for the job
        try:
            container_path = tempfile.mkdtemp(dir=self._tmp_dir)
        except Exception as e:
            self._logger.error("Cannot make container temp directory! %s", str(e), exc_info=True)
            for p in ports:
                self._external_ports.add(ports[p])
            raise CannotCreateJobException('Cannot make container temp directory.')

        task_path = path_join(container_path, 'task')  # tmp_dir/id/task/
        course_path = path_join(container_path, 'course')

        sockets_path = path_join(container_path, 'sockets')  # tmp_dir/id/socket/
        student_path = path_join(task_path, 'student')  # tmp_dir/id/task/student/
        systemfiles_path = path_join(task_path, 'systemfiles')  # tmp_dir/id/task/systemfiles/

        course_common_path = path_join(course_path, 'common')
        course_common_student_path = path_join(course_path, 'common', 'student')

        # Create the needed directories
        os.mkdir(sockets_path)
        os.chmod(container_path, 0o777)
        os.chmod(sockets_path, 0o777)
        os.mkdir(course_path)

        # TODO: avoid copy
        task_fs.copy_from(None, task_path)
        os.chmod(task_path, 0o777)

        if not os.path.exists(student_path):
            os.mkdir(student_path)
            os.chmod(student_path, 0o777)

        # Copy common and common/student if needed
        # TODO: avoid copy
        if course_fs.from_subfolder("$common").exists():
            course_fs.from_subfolder("$common").copy_from(None, course_common_path)
        else:
            os.mkdir(course_common_path)

        if course_fs.from_subfolder("$common").from_subfolder("student").exists():
            course_fs.from_subfolder("$common").from_subfolder("student").copy_from(None, course_common_student_path)
        else:
            os.mkdir(course_common_student_path)

        # Run the container
        try:
            container_id = self._docker.sync.create_container(environment, enable_network, mem_limit, task_path,
                                                              sockets_path, course_common_path,
                                                              course_common_student_path, ports)
        except Exception as e:
            self._logger.warning("Cannot create container! %s", str(e), exc_info=True)
            shutil.rmtree(container_path)
            for p in ports:
                self._external_ports.add(ports[p])
            raise CannotCreateJobException('Cannot create container.')

        # Store info
        self._containers_running[container_id] = message, container_path, future_results
        self._container_for_job[message.job_id] = container_id
        self._student_containers_for_job[message.job_id] = set()

        if len(ports) != 0:
            self._assigned_external_ports[container_id] = list(ports.values())

        try:
            # Start the container
            self._docker.sync.start_container(container_id)
        except Exception as e:
            self._logger.warning("Cannot start container! %s", str(e), exc_info=True)
            shutil.rmtree(container_path)
            for p in ports:
                self._external_ports.add(ports[p])

            raise CannotCreateJobException('Cannot start container')

        return {
            "job_id": message.job_id,
            "container_id": container_id,
            "inputdata": message.inputdata,
            "debug": debug,
            "ports": ports,
            "orig_env": environment_name,
            "orig_memory_limit": mem_limit,
            "orig_time_limit": time_limit,
            "orig_hard_time_limit": hard_time_limit,
            "sockets_path": sockets_path,
            "student_path": student_path,
            "systemfiles_path": systemfiles_path,
            "course_common_student_path": course_common_student_path,
            "run_cmd": run_cmd
        }

    async def new_job(self, message: BackendNewJob):
        """
        Handles a new job: starts the grading container
        """
        self._logger.info("Received request for jobid %s", message.job_id)
        future_results = asyncio.Future()
        out = await self._loop.run_in_executor(None, lambda: self.__new_job_sync(message, future_results))
        self._create_safe_task(self.handle_running_container(**out, future_results=future_results))
        await self._timeout_watcher.register_container(out["container_id"], out["orig_time_limit"], out["orig_hard_time_limit"])

    async def create_student_container(self, job_id, parent_container_id, sockets_path, student_path, systemfiles_path,
                                       course_common_student_path, socket_id,  environment_name, memory_limit,
                                       time_limit, hard_time_limit, share_network, write_stream):
        """
        Creates a new student container.
        :param write_stream: stream on which to write the return value of the container (with a correctly formatted msgpack message)
        """
        try:
            self._logger.debug("Starting new student container... %s %s %s %s", environment_name, memory_limit, time_limit, hard_time_limit)

            if environment_name not in self._containers:
                self._logger.warning("Student container asked for an unknown environment %s (not in aliases)", environment_name)
                await self._write_to_container_stdin(write_stream, {"type": "run_student_retval", "retval": 254, "socket_id": socket_id})
                return

            environment = self._containers[environment_name]["id"]

            try:
                socket_path = path_join(sockets_path, str(socket_id) + ".sock")
                container_id = await self._docker.create_container_student(parent_container_id, environment, share_network,
                                                                           memory_limit, student_path, socket_path,
                                                                           systemfiles_path, course_common_student_path)
            except Exception as e:
                self._logger.exception("Cannot create student container!")
                await self._write_to_container_stdin(write_stream, {"type": "run_student_retval", "retval": 254, "socket_id": socket_id})

                if isinstance(e, asyncio.CancelledError):
                    raise

                return

            self._student_containers_for_job[job_id].add(container_id)
            self._student_containers_running[container_id] = job_id, parent_container_id, socket_id, write_stream

            # send to the container that the sibling has started
            await self._write_to_container_stdin(write_stream, {"type": "run_student_started", "socket_id": socket_id})

            try:
                await self._docker.start_container(container_id)
            except Exception as e:
                self._logger.exception("Cannot start student container!")
                await self._write_to_container_stdin(write_stream, {"type": "run_student_retval", "retval": 254, "socket_id": socket_id})

                if isinstance(e, asyncio.CancelledError):
                    raise

                return

            # Verify the time limit
            await self._timeout_watcher.register_container(container_id, time_limit, hard_time_limit)
        except asyncio.CancelledError:
            raise
        except:
            self._logger.exception("Exception in create_student_container")

    async def _write_to_container_stdin(self, write_stream, message):
        """
        Send a message to the stdin of a container, with the right data
        :param write_stream: asyncio write stream to the stdin of the container
        :param message: dict to be msgpacked and sent
        """
        msg = msgpack.dumps(message, use_bin_type=True)
        self._logger.debug("Sending %i bytes to container", len(msg))
        write_stream.write(struct.pack('I', len(msg)))
        write_stream.write(msg)
        await write_stream.drain()

    async def handle_running_container(self, job_id, container_id, inputdata, run_cmd, debug, ports, orig_env,
                                       orig_memory_limit, orig_time_limit, orig_hard_time_limit, sockets_path,
                                       student_path, systemfiles_path, course_common_student_path, future_results):
        """ Talk with a container. Sends the initial input. Allows to start student containers """
        sock = await self._docker.attach_to_container(container_id)
        try:
            read_stream, write_stream = await asyncio.open_connection(sock=sock.get_socket())
        except asyncio.CancelledError:
            raise
        except:
            self._logger.exception("Exception occurred while creating read/write stream to container")
            return None

        # Send hello msg
        hello_msg = {"type": "start", "input": inputdata, "debug": debug}
        if run_cmd is not None:
            hello_msg["run_cmd"] = run_cmd
        await self._write_to_container_stdin(write_stream, hello_msg)
        result = None

        buffer = bytearray()
        try:
            while not read_stream.at_eof():
                msg_header = await read_stream.readexactly(8)
                outtype, length = struct.unpack_from('>BxxxL', msg_header)  # format imposed by docker in the attach endpoint
                if length != 0:
                    content = await read_stream.readexactly(length)
                    if outtype == 1:  # stdout
                        buffer += content

                    if outtype == 2:  # stderr
                        self._logger.debug("Received stderr from containers:\n%s", content)

                    # 4 first bytes are the length of the message. If we have a complete message...
                    while len(buffer) > 4 and len(buffer) >= 4+struct.unpack('I',buffer[0:4])[0]:
                        msg_encoded = buffer[4:4 + struct.unpack('I', buffer[0:4])[0]]  # ... get it
                        buffer = buffer[4 + struct.unpack('I', buffer[0:4])[0]:]  # ... withdraw it from the buffer
                        try:
                            msg = msgpack.unpackb(msg_encoded, use_list=False)
                            self._logger.debug("Received msg %s from container %s", msg["type"], container_id)
                            if msg["type"] == "run_student":
                                # start a new student container
                                environment = msg["environment"] or orig_env
                                memory_limit = min(msg["memory_limit"] or orig_memory_limit, orig_memory_limit)
                                time_limit = min(msg["time_limit"] or orig_time_limit, orig_time_limit)
                                hard_time_limit = min(msg["hard_time_limit"] or orig_hard_time_limit, orig_hard_time_limit)
                                share_network = msg["share_network"]
                                socket_id = msg["socket_id"]
                                assert "/" not in socket_id  # ensure task creator do not try to break the agent :-(
                                self._create_safe_task(self.create_student_container(job_id, container_id, sockets_path, student_path,
                                                                                     systemfiles_path, course_common_student_path,
                                                                                     socket_id,  environment, memory_limit, time_limit,
                                                                                     hard_time_limit, share_network, write_stream))
                            elif msg["type"] == "ssh_key":
                                # send the data to the backend (and client)
                                self._logger.info("%s %s", container_id, str(msg))
                                await self.send_ssh_job_info(job_id, self._address_host, ports[22], msg["ssh_key"])
                            elif msg["type"] == "result":
                                # last message containing the results of the container
                                result = msg["result"]
                        except:
                            self._logger.exception("Received incorrect message from container %s (job id %s)", container_id, job_id)
        except asyncio.IncompleteReadError:
            self._logger.debug("Container output ended with an IncompleteReadError; It was probably killed.")
        except asyncio.CancelledError:
            write_stream.close()
            sock.close_socket()
            future_results.set_result(result)
            raise
        except:
            self._logger.exception("Exception while reading container %s output", container_id)

        write_stream.close()
        sock.close_socket()
        future_results.set_result(result)

        if not result:
            self._logger.warning("Container %s has not given any result", container_id)

    async def handle_student_job_closing(self, container_id, retval):
        """
        Handle a closing student container. Do some cleaning, verify memory limits, timeouts, ... and returns data to the associated grading
        container
        """
        try:
            self._logger.debug("Closing student %s", container_id)
            try:
                job_id, parent_container_id, socket_id, write_stream = self._student_containers_running[container_id]
                del self._student_containers_running[container_id]
            except asyncio.CancelledError:
                raise
            except:
                self._logger.warning("Student container %s that has finished(p1) was not launched by this agent", str(container_id), exc_info=True)
                return

            # Delete remaining student containers
            if job_id in self._student_containers_for_job:  # if it does not exists, then the parent container has closed
                self._student_containers_for_job[job_id].remove(container_id)

            killed = await self._timeout_watcher.was_killed(container_id)
            if container_id in self._containers_killed:
                killed = self._containers_killed[container_id]
                del self._containers_killed[container_id]

            if killed == "timeout":
                retval = 253
            elif killed == "overflow":
                retval = 252

            try:
                await self._write_to_container_stdin(write_stream, {"type": "run_student_retval", "retval": retval, "socket_id": socket_id})
            except asyncio.CancelledError:
                raise
            except:
                pass  # parent container closed

            # Do not forget to remove the container
            try:
                await self._docker.remove_container(container_id)
            except asyncio.CancelledError:
                raise
            except:
                pass  # ignore
        except asyncio.CancelledError:
            raise
        except:
            self._logger.exception("Exception in handle_student_job_closing")

    async def handle_job_closing(self, container_id, retval):
        """
        Handle a closing student container. Do some cleaning, verify memory limits, timeouts, ... and returns data to the backend
        """
        try:
            self._logger.debug("Closing %s", container_id)
            try:
                message, container_path, future_results = self._containers_running[container_id]
                del self._containers_running[container_id]
            except asyncio.CancelledError:
                raise
            except:
                self._logger.warning("Container %s that has finished(p1) was not launched by this agent", str(container_id), exc_info=True)
                return

            # Close sub containers
            for student_container_id_loop in self._student_containers_for_job[message.job_id]:
                # little hack to ensure the value of student_container_id_loop is copied into the closure
                async def close_and_delete(student_container_id=student_container_id_loop):
                    try:
                        await self._docker.kill_container(student_container_id)
                        await self._docker.remove_container(student_container_id)
                    except asyncio.CancelledError:
                        raise
                    except:
                        pass  # ignore
                self._create_safe_task(close_and_delete(student_container_id_loop))
            del self._student_containers_for_job[message.job_id]

            # Allow other container to reuse the external ports this container has finished to use
            if container_id in self._assigned_external_ports:
                for p in self._assigned_external_ports[container_id]:
                    self._external_ports.add(p)
                del self._assigned_external_ports[container_id]

            # Verify if the container was killed, either by the client, by an OOM or by a timeout
            killed = await self._timeout_watcher.was_killed(container_id)
            if container_id in self._containers_killed:
                killed = self._containers_killed[container_id]
                del self._containers_killed[container_id]

            stdout = ""
            stderr = ""
            result = "crash" if retval == -1 else None
            error_msg = None
            grade = None
            problems = {}
            custom = {}
            tests = {}
            archive = None
            state = ""

            if killed is not None:
                result = killed

            # If everything did well, continue to retrieve the status from the container
            if result is None:
                # Get logs back
                try:
                    return_value = await future_results

                    # Accepted types for return dict
                    accepted_types = {"stdout": str, "stderr": str, "result": str, "text": str, "grade": float,
                                      "problems": dict, "custom": dict, "tests": dict, "state": str, "archive": str}

                    keys_fct = {"problems": id_checker, "custom": id_checker, "tests": id_checker_tests}

                    # Check dict content
                    for key, item in return_value.items():
                        if not isinstance(item, accepted_types[key]):
                            raise Exception("Feedback file is badly formatted.")
                        elif accepted_types[key] == dict and key != "custom": #custom can contain anything:
                            for sub_key, sub_item in item.items():
                                if not keys_fct[key](sub_key) or isinstance(sub_item, dict):
                                    raise Exception("Feedback file is badly formatted.")

                    # Set output fields
                    stdout = return_value.get("stdout", "")
                    stderr = return_value.get("stderr", "")
                    result = return_value.get("result", "error")
                    error_msg = return_value.get("text", "")
                    grade = return_value.get("grade", None)
                    problems = return_value.get("problems", {})
                    custom = return_value.get("custom", {})
                    tests = return_value.get("tests", {})
                    state = return_value.get("state", "")
                    archive = return_value.get("archive", None)
                    if archive is not None:
                        archive = base64.b64decode(archive)
                except Exception as e:
                    self._logger.exception("Cannot get back output of container %s! (%s)", container_id, str(e))
                    result = "crash"
                    error_msg = 'The grader did not return a readable output : {}'.format(str(e))

            # Default values
            if error_msg is None:
                error_msg = ""
            if grade is None:
                if result == "success":
                    grade = 100.0
                else:
                    grade = 0.0

            # Remove container
            try:
                await self._docker.remove_container(container_id)
            except asyncio.CancelledError:
                raise
            except:
                pass

            # Delete folders
            try:
                await self._ashutil.rmtree(container_path)
            except PermissionError:
                self._logger.debug("Cannot remove old container path!")
                pass  # todo: run a docker container to force removal

            # Return!
            await self.send_job_result(message.job_id, result, error_msg, grade, problems, tests, custom, state, archive, stdout, stderr)

            # Do not forget to remove data from internal state
            del self._container_for_job[message.job_id]
        except asyncio.CancelledError:
            raise
        except:
            self._logger.exception("Exception in handle_job_closing")

    async def kill_job(self, message: BackendKillJob):
        """ Handles `kill` messages. Kill things. """
        try:
            if message.job_id in self._container_for_job:
                self._containers_killed[self._container_for_job[message.job_id]] = "killed"
                await self._docker.kill_container(self._container_for_job[message.job_id])
            else:
                self._logger.warning("Cannot kill container for job %s because it is not running", str(message.job_id))
                # Ensure the backend/frontend receive the info that the job is done. This will be ignored in the worst
                # case.
                await self.send_job_result(message.job_id, "killed")
        except asyncio.CancelledError:
            raise
        except:
            self._logger.exception("Exception in handle_kill_job")

    async def run(self):
        await self._init_clean()

        # Init Docker events watcher
        watcher_docker_event = self._create_safe_task(self._watch_docker_events())

        try:
            await super(DockerAgent, self).run()
        except:
            await self._end_clean()
            raise
