# Copyright 2016 Mycroft AI, Inc.
#
# This file is part of Mycroft Core.
#
# Mycroft Core is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Mycroft Core is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Mycroft Core.  If not, see <http://www.gnu.org/licenses/>.


import json
import os
import subprocess
import sys
import time
from os.path import exists, join
from threading import Timer, Thread, Event

from mycroft import MYCROFT_ROOT_PATH
from mycroft.configuration import ConfigurationManager
from mycroft.lock import Lock  # Creates PID file for single instance
from mycroft.messagebus.client.ws import WebsocketClient
from mycroft.messagebus.message import Message
from mycroft.skills.core import load_skill, create_skill_descriptor, \
    MainModule, FallbackSkill
from mycroft.skills.intent_service import IntentService
from mycroft.skills.padatious_service import PadatiousService
from mycroft.util import connected
from mycroft.util.log import getLogger
from mycroft.api import is_paired
import mycroft.dialog
from mycroft import MYCROFT_ROOT_PATH

logger = getLogger("Skills")

__author__ = 'seanfitz'

pairing = False
msm = False

ws = None
loaded_skills = {}
last_modified_skill = 0
skill_reload_thread = None
skills_manager_timer = None
id_counter = 0

skills_config = ConfigurationManager.instance().get("skills")
PRIORITY_SKILLS = skills_config["priority_skills"]
BLACKLISTED_SKILLS = skills_config["blacklisted_skills"]

SKILLS_DIR = skills_config.get("directory")
if SKILLS_DIR is None or SKILLS_DIR == "default":
    SKILLS_DIR = join(MYCROFT_ROOT_PATH, "jarbas_skills")

# TODO remove this, only for dev testing
# SKILLS_DIR = join(MYCROFT_ROOT_PATH, "mycroft/jarbas_skills")

DEFAULT_SKILLS = skills_config.get("msm_skills",
                                   ["skill-alarm", "skill-audio-record",
                                    "skill-date-time",
                                    "skill-desktop-launcher",
                                    "skill-ip", "skill-joke",
                                    "skill-hello-world",
                                    "skill-media",
                                    "skill-naptime", "skill-personal",
                                    "skill-playback-control",
                                    "skill-reminder",
                                    "skill-installer", "skill-singing",
                                    "skill-speak",
                                    "skill-spelling", "skill-stop",
                                    "skill-stock",
                                    "skill-volume"])

installer_config = ConfigurationManager.instance().get("SkillInstallerSkill")
MSM_PATH = installer_config.get("path", join(MYCROFT_ROOT_PATH, 'msm'))
MSM_BIN = join(MSM_PATH, 'msm')


def msm_skills_dir():
    try:
        if not exists(SKILLS_DIR):
            os.mkdir(SKILLS_DIR)
        LOG.info("updating msm SKILLS_DIR from config")
        msm_skills = join(MSM_PATH, "msm_skills_path")
        with open(msm_skills, "w") as f:
            f.write(SKILLS_DIR)
        LOG.info("updating msm DEFAULT_SKILLS from config")
        msm_defaults = join(MSM_PATH, "msm_skills_defaults")
        with open(msm_defaults, "w") as f:
            for skill in DEFAULT_SKILLS:
                f.write(skill + " ")
    except Exception as e:
        LOG.error(e)


def connect():
    global ws
    ws.run_forever()


def install_default_skills(speak=True):
    """
        Install default skill set using msm.

        Args:
            speak (optional): Enable response for success. Default True
    """
    msm_skills_dir()
    if exists(MSM_BIN):
        p = subprocess.Popen(MSM_BIN + " default", stderr=subprocess.STDOUT,
                             stdout=subprocess.PIPE, shell=True)
        (output, err) = p.communicate()
        res = p.returncode
        if res == 0 and speak:
            # ws.emit(Message("speak", {
            #     'utterance': mycroft.dialog.get("skills updated")}))
            pass
        elif not connected():
            logger.error('msm failed, network connection is not available')
            ws.emit(Message("speak", {
                'utterance': mycroft.dialog.get("no network connection")}))
        elif res != 0:
            logger.error('msm failed with error {}: {}'.format(res, output))
            ws.emit(Message("speak", {
                'utterance': mycroft.dialog.get(
                    "sorry I couldn't install default skills")}))

    else:
        logger.error("Unable to invoke Mycroft Skill Manager: " + MSM_BIN)


def skills_manager(message):
    """
        skills_manager runs on a Timer every hour and checks for updated
        skills.
    """
    global skills_manager_timer

    if msm:
        if connected():
            if skills_manager_timer is None:
                 pass
            # Install default skills and look for updates via Github
            logger.debug("==== Invoking Mycroft Skill Manager: " + MSM_BIN)
            install_default_skills(False)

    # Perform check again once and hour
    skills_manager_timer = Timer(3600, _skills_manager_dispatch)
    skills_manager_timer.daemon = True
    skills_manager_timer.start()


def _skills_manager_dispatch():
    """
        Thread function to trigger skill_manager over message bus.
    """
    global ws
    ws.emit(Message("skill_manager", {}))


def _starting_up():
    """
        Start loading skills.

        Starts
        - reloading of skills when needed
        - a timer to check for internet connection
        - a timer for updating skills every hour
        - adapt intent service
        - padatious intent service
    """
    global ws, skill_reload_thread

    check_connection()

    ws.on('intent_failure', FallbackSkill.make_intent_failure_handler(ws))

    # Create skill_manager listener and invoke the first time
    if msm:
        ws.on('skill_manager', skills_manager)
        ws.emit(Message('skill_manager', {}))
        ws.on('mycroft.internet.connected', install_default_skills)


    # Create the Intent manager, which converts utterances to intents
    # This is the heart of the voice invoked skill system

    PadatiousService(ws)
    IntentService(ws)

    # Create a thread that monitors the loaded skills, looking for updates
    skill_reload_thread = WatchSkills()
    skill_reload_thread.daemon = True
    skill_reload_thread.start()


def check_connection():
    """
        Check for network connection. If not paired trigger pairing.
        Runs as a Timer every second until connection is detected.
    """
    if connected():
        ws.emit(Message('mycroft.internet.connected'))
        # check for pairing, if not automatically start pairing
        if pairing:
            if not is_paired():
                # begin the process
                payload = {
                    'utterances': ["pair my device"],
                    'lang': "en-us"
                }
                ws.emit(Message("recognizer_loop:utterance", payload,
                                {"source": "skills"}))
    else:
        thread = Timer(1, check_connection)
        thread.daemon = True
        thread.start()


def _get_last_modified_date(path):
    """
        Get last modified date excluding compiled python files, hidden
        directories and the settings.json file.

        Arg:
            path:   skill directory to check
        Returns:    time of last change
    """
    last_date = 0
    root_dir, subdirs, files = os.walk(path).next()
    # get subdirs and remove hidden ones
    subdirs = [s for s in subdirs if not s.startswith('.')]
    for subdir in subdirs:
        for root, _, _ in os.walk(join(path, subdir)):
            base = os.path.basename(root)
            # checking if is a hidden path
            if not base.startswith(".") and not base.startswith("/."):
                last_date = max(last_date, os.path.getmtime(root))

    # check files of interest in the skill root directory
    files = [f for f in files
             if not f.endswith('.pyc') and f != 'settings.json']
    for f in files:
        last_date = max(last_date, os.path.getmtime(os.path.join(path, f)))
    return last_date


def load_priority():
    global ws, loaded_skills, SKILLS_DIR, PRIORITY_SKILLS, id_counter

    if exists(SKILLS_DIR):
        for skill_folder in PRIORITY_SKILLS:
            try:
                skill = loaded_skills.get(skill_folder)
                skill["path"] = os.path.join(SKILLS_DIR, skill_folder)
                # checking if is a skill
                if not MainModule + ".py" in os.listdir(skill["path"]):
                    continue
                # getting the newest modified date of skill
                skill["last_modified"] = _get_last_modified_date(skill["path"])
                # checking if skill is loaded
                if skill.get("loaded"):
                    continue

                skill["instance"] = load_skill(
                    create_skill_descriptor(skill["path"]), ws, skill["id"])
                skill["loaded"] = True
            except TypeError:
                logger.error(skill_folder + " does not seem to exist")


class WatchSkills(Thread):
    """
        Thread function to reload skills when a change is detected.
    """
    def __init__(self):
        super(WatchSkills, self).__init__()
        self._stop_event = Event()

    def run(self):
        global ws, loaded_skills, last_modified_skill, \
            id_counter

        # Scan the folder that contains Skills.
        list = filter(lambda x: os.path.isdir(
            os.path.join(SKILLS_DIR, x)), os.listdir(SKILLS_DIR))
        for skill_folder in list:
            if skill_folder not in loaded_skills:
                # register unique ID
                id_counter += 1
                loaded_skills[skill_folder] = {"id": id_counter,
                                               "loaded": False,
                                               "do_not_reload": False,
                                               "do_not_load": False,
                                               "reload_request": False,
                                               "shutdown": False}

        # Load priority skills first
        load_priority()

        # Scan the file folder that contains Skills.  If a Skill is updated,
        # unload the existing version from memory and reload from the disk.
        while not self._stop_event.is_set():
            if exists(SKILLS_DIR):
                # checking skills dir and getting all skills there
                list = filter(lambda x: os.path.isdir(
                    os.path.join(SKILLS_DIR, x)), os.listdir(SKILLS_DIR))

                for skill_folder in list:
                    if skill_folder in BLACKLISTED_SKILLS:
                        continue

                    if skill_folder not in loaded_skills:
                        # check if its a new skill just added to skills_folder
                        id_counter += 1
                        loaded_skills[skill_folder] = {"id": id_counter,
                                                       "loaded": False,
                                                       "do_not_reload": False,
                                                       "do_not_load": False,
                                                       "reload_request": False,
                                                       "shutdown": False}
                    skill = loaded_skills.get(skill_folder)
                    # see if this skill was supposed to be shutdown
                    if skill["shutdown"]:
                        logger.debug(
                            "Skill " + skill_folder + " shutdown was requested")
                        skill["shutdown"] = False
                        if skill.get("loaded"):
                            if skill.get("instance"):
                                if skill["instance"].external_shutdown:
                                    skill["instance"].shutdown()
                                    del skill["instance"]
                                    skill["loaded"] = False
                                    ws.emit(Message("shutdown_skill_response",
                                                    {"status": "shutdown",
                                                     "skill_id": skill["id"]}))
                                    continue
                                else:
                                    ws.emit(
                                        Message("shutdown_skill_response",
                                                {"status": "forbidden",
                                                 "skill_id": skill["id"]}))
                                    logger.debug(
                                        "External shutdown for " + skill_folder + " is forbidden")
                                    continue
                        else:
                            ws.emit(Message("shutdown_skill_response",
                                            {"status": "shutdown",
                                             "skill_id": skill["id"]}))
                            logger.debug(skill_folder + " already shutdown")
                            continue
                    # check if we are supposed to load this skill
                    elif skill["do_not_load"]:
                        continue
                    skill["path"] = os.path.join(SKILLS_DIR, skill_folder)
                    # checking if is a skill
                    if not MainModule + ".py" in os.listdir(skill["path"]):
                        continue
                    # getting the newest modified date of skill
                    skill["last_modified"] = _get_last_modified_date(
                        skill["path"])
                    modified = skill.get("last_modified", 0)

                    # checking if skill is loaded and wasn't modified
                    if skill.get(
                            "loaded") and (
                            modified <= last_modified_skill and not skill[
                        "reload_request"]):
                        continue
                    # checking if skill was modified or reload was requested
                    elif skill.get(
                            "instance") and (
                            modified > last_modified_skill or skill[
                        "reload_request"]):
                        # checking if skill reload was requested
                        if skill["reload_request"]:
                            logger.debug(
                                "External reload for " + skill_folder + " requested")
                            loaded_skills[skill_folder][
                                "reload_request"] = False
                            if skill["instance"].external_reload:
                                ws.emit(Message("reload_skill_response",
                                                {"status": "reloading",
                                                 "skill_id": skill["id"]}))
                                skill["do_not_reload"] = False
                            else:
                                ws.emit(Message("reload_skill_response",
                                                {"status": "forbidden",
                                                 "skill_id": skill["id"]}))
                                logger.debug(
                                    "External reload for " + skill_folder + " is forbidden")
                                skill["do_not_reload"] = True
                        # check if skills allows auto_reload
                        elif not skill["instance"].reload_skill:
                            continue
                        else:
                            skill["do_not_reload"] = False
                        # check if we are suposed to reload skill
                        if not skill["do_not_reload"]:
                            logger.debug("Reloading Skill: " + skill_folder)
                            # removing listeners and stopping threads
                            if skill.get("instance") is not None:
                                logger.debug(
                                    "Shutting down Skill: " + skill_folder)
                                skill["instance"].shutdown()
                                del skill["instance"]
                            else:
                                logger.debug(
                                    "Skill " + skill_folder + " is already shutdown")

                    # load skill
                    if not skill["do_not_reload"]:
                        skill["loaded"] = True
                        skill["instance"] = load_skill(
                            create_skill_descriptor(skill["path"]), ws,
                            skill["id"])

                        if skill["instance"]:
                            ws.emit(Message("skill.loaded",
                                            {"skill": skill["id"]}))
                        else:
                            ws.emit(Message("skill.loaded.fail",
                                            {"skill": skill["id"]}))
                            skill["do_not_load"] = True
                    loaded_skills[skill_folder] = skill

            # get the last modified skill
            modified_dates = map(lambda x: x.get("last_modified"),
                                 loaded_skills.values())
            if len(modified_dates) > 0:
                last_modified_skill = max(modified_dates)

            # Pause briefly before beginning next scan
            time.sleep(2)

    def stop(self):
        self._stop_event.set()


def handle_shutdown_skill_request(message):
    global loaded_skills
    skill_id = message.data["skill_id"]
    for skill in loaded_skills:
        if loaded_skills[skill]["id"] == skill_id:
            # avoid auto-reload
            loaded_skills[skill]["do_not_load"] = True
            loaded_skills[skill]["shutdown"] = True
            loaded_skills[skill]["reload_request"] = False
            # loaded_skills[skill]["loaded"] = False
            ws.emit(Message("shutdown_skill_response", {"status": "waiting", "skill_id": skill_id}))
            break


def handle_reload_skill_request(message):
    global loaded_skills, ws
    skill_id = message.data["skill_id"]
    for skill in loaded_skills:
        if loaded_skills[skill]["id"] == skill_id:
            loaded_skills[skill]["reload_request"] = True
            loaded_skills[skill]["do_not_load"] = False
            loaded_skills[skill]["shutdown"] = False
            loaded_skills[skill]["loaded"] = False
            ws.emit(Message("reload_skill_response", {"status": "waiting", "skill_id": skill_id}))
            break


def handle_conversation_request(message):
    skill_id = int(message.data["skill_id"])
    utterances = message.data["utterances"]
    lang = message.data["lang"]
    global ws, loaded_skills
    # loop trough skills list and call converse for skill with skill_id
    for skill in loaded_skills:
        if loaded_skills[skill]["id"] == skill_id:
            try:
                instance = loaded_skills[skill]["instance"]
            except:
                logger.error("converse requested but skill not loaded")
                ws.emit(Message("skill.converse.response", {
                    "skill_id": 0, "result": False}))
                return
            try:
                result = instance.converse(utterances, lang)
                ws.emit(Message("skill.converse.response", {
                    "skill_id": skill_id, "result": result}))
                return
            except:
                logger.error("Converse method malformed for skill " + str(skill_id))
    ws.emit(Message("skill.converse.response", {
        "skill_id": 0, "result": False}))


def handle_loaded_skills_request(message):
    global ws, loaded_skills
    skills = []
    # loop trough skills list
    for skill in loaded_skills:
        loaded = {}
        loaded.setdefault("folder", skill)
        try:
            loaded.setdefault("name", loaded_skills[skill]["instance"].name)
        except:
            loaded.setdefault("name", "unloaded")
        loaded.setdefault("id", loaded_skills[skill]["id"])
        skills.append(loaded)
    ws.emit(Message("loaded_skills_response", {"skills": skills}))


def main():
    global ws
    lock = Lock('skills')  # prevent multiple instances of this service

    # Connect this Skill management process to the websocket
    ws = WebsocketClient()
    ConfigurationManager.init(ws)

    ignore_logs = ConfigurationManager.instance().get("ignore_logs")

    # Listen for messages and echo them for logging
    def _echo(message):
        try:
            _message = json.loads(message)

            if _message.get("type") in ignore_logs:
                return

            if _message.get("type") == "registration":
                # do not log tokens from registration messages
                _message["data"]["token"] = None
            message = json.dumps(_message)
        except:
            pass
        logger.debug(message)

    ws.on('message', _echo)
    ws.once('open', _starting_up)
    ws.on('skill.converse.request', handle_conversation_request)
    ws.on('reload_skill_request', handle_reload_skill_request)
    ws.on('shutdown_skill_request', handle_shutdown_skill_request)
    ws.on('loaded_skills_request', handle_loaded_skills_request)
    ws.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Do a clean shutdown of all skills and terminate all running threads
        for skill in loaded_skills:
            try:
                loaded_skills[skill]['instance'].shutdown()
            except:
                pass
        if skills_manager_timer:
            skills_manager_timer.cancel()
        if skill_reload_thread:
            skill_reload_thread.stop()
            skill_reload_thread.join()

    finally:
        sys.exit()
