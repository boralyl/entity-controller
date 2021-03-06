"""
Entity controller component for Home Assistant.
Maintainer:       Daniel Mason
Version:          v3.1.1
Documentation:    https://github.com/danobot/entity-controller
Issues Tracker:   Report issues on Github. Ensure you have the latest version. Include:
                    * YAML configuration (for the misbehaving entity)
                    * log entries at time of error and at time of initialisation
"""
import logging
import voluptuous as vol
import homeassistant.helpers.config_validation as cv

from homeassistant.helpers import entity, service, event
from homeassistant.const import (
    SUN_EVENT_SUNSET, SUN_EVENT_SUNRISE)
from homeassistant.util import dt
from homeassistant.helpers.entity_component import EntityComponent
import logging
from transitions import Machine
from transitions.extensions import HierarchicalMachine as Machine
from threading import Timer
from datetime import datetime, timedelta, date, time
import re
from homeassistant.helpers.sun import get_astral_event_date

DEPENDENCIES = ['light', 'sensor', 'binary_sensor', 'cover', 'fan',
                'media_player']
REQUIREMENTS = ['transitions==0.6.9']

DOMAIN = 'entity_controller'
CONSTRAIN_START = 1
CONSTRAIN_END = 2

VERSION = '3.1.1'
SENSOR_TYPE_DURATION = 'duration'
SENSOR_TYPE_EVENT = 'event'
MODE_DAY = 'day'
MODE_NIGHT = 'night'

DEFAULT_DELAY = 180
DEFAULT_BRIGHTNESS = 100
DEFAULT_NAME = 'Entity Timer'

CONF_NAME = 'name'
CONF_CONTROL = 'entities'
CONF_SENSORS = 'sensors'
CONF_STATE = 'state_entities'
CONF_DELAY = 'delay'
CONF_NIGHT_MODE = 'night_mode'
CONFIG_START_TIME = 'start_time'
CONFIG_END_TIME = 'end_time'
STATES = ['idle', 'overridden', 'constrained', 'blocked',
          {'name': 'active', 'children': ['timer', 'stay_on'],
           'initial': False}]

_LOGGER = logging.getLogger(__name__)
devices = []


async def async_setup(hass, config):
    """Load graph configurations."""

    component = EntityComponent(
        _LOGGER, DOMAIN, hass)

    myconfig = config[DOMAIN]

    _LOGGER.info("If you have ANY issues with EntityController, please enable DEBUG logging under the logger component and kindly report the issue on Github. https://github.com/danobot/entity-controller/issues")
    _LOGGER.info("Domain Configuration: " + str(myconfig))

    machine = Machine(states=STATES,
                      initial='idle',
                      # title=self.name+" State Diagram",
                      # show_conditions=True
                      # show_auto_transitions = True,
                      finalize_event='finalize'
                      )

    machine.add_transition(trigger='constrain', source='*', dest='constrained')
    machine.add_transition(trigger='override',
                           source=['idle', 'active_timer', 'blocked'],
                           dest='overridden')

    # Idle
    # machine.add_transition(trigger='sensor_off',           source='idle',              dest=None)
    machine.add_transition(trigger='sensor_on', source='idle', dest='active',
                           conditions=['is_state_entities_off'])
    machine.add_transition(trigger='sensor_on', source='idle', dest='blocked',
                           conditions=['is_state_entities_on'])

    # Blocked
    machine.add_transition(trigger='enable', source='blocked', dest='idle')
    machine.add_transition(trigger='sensor_on', source='blocked',
                           dest='blocked')  # re-entering self-transition (on_enter callback executed.)

    # Overridden      
    machine.add_transition(trigger='enable', source='overridden', dest='idle')

    # machine.add_transition(trigger='sensor_off',           source=['overridden'],          dest=None)

    machine.add_transition(trigger='enter', source='active',
                           dest='active_timer', unless='will_stay_on')
    machine.add_transition(trigger='enter', source='active',
                           dest='active_stay_on', conditions='will_stay_on')

    # Active Timer
    machine.add_transition(trigger='sensor_on', source='active_timer',
                           dest=None, after='_reset_timer')
    # machine.add_transition(trigger='sensor_off',           source='active_timer',      dest=None,              conditions=['is_event_sensor'])
    machine.add_transition(trigger='sensor_off_duration',
                           source='active_timer', dest='idle',
                           conditions=['is_timer_expired'])
    machine.add_transition(trigger='timer_expires', source='active_timer',
                           dest='idle', conditions=['is_event_sensor'])
    machine.add_transition(trigger='timer_expires', source='active_timer',
                           dest='idle',
                           conditions=['is_duration_sensor', 'is_sensor_off'])
    machine.add_transition(trigger='block_timer_expires', source='blocked',
                           dest='idle')
    machine.add_transition(trigger='control', source='active_timer',
                           dest='idle', conditions=['is_state_entities_off'])

    # machine.add_transition(trigger='sensor_off',           source='active_stay_on',    dest=None)
    machine.add_transition(trigger='timer_expires', source='active_stay_on',
                           dest=None)

    # Constrained
    machine.add_transition(trigger='enable', source='constrained', dest='idle', conditions=['is_override_state_off'])
    machine.add_transition(trigger='enable', source='constrained', dest='overridden', conditions=['is_override_state_on'])

    for key, config in myconfig.items():
        _LOGGER.info("Config Item %s: %s", str(key), str(config))
        config["name"] = key
        m = None
        m = EntityController(hass, config, machine)
        # machine.add_model(m.model)
        # m.model.after_model(config)
        devices.append(m)

    await component.async_add_entities(devices)

    _LOGGER.info("The %s component is ready!", DOMAIN)

    return True


class EntityController(entity.Entity):

    def __init__(self, hass, config, machine):
        self.attributes = {}
        self.may_update = False
        self.model = None
        self.friendly_name = config.get('name', 'Motion Light')
        if 'friendly_name' in config:
            self.friendly_name = config.get('friendly_name')
        try:
            self.model = Model(hass, config, machine, self)
        except AttributeError as e:
            self.log.error(
                "Configuration error! Please ensure you use plural keys for lists. e.g. sensors, entities")
        event.async_call_later(hass, 1, self.do_update)

    @property
    def state(self):
        """Return the state of the entity."""
        return self.model.state

    @property
    def name(self):
        """Return the state of the entity."""
        return self.friendly_name

    @property
    def icon(self):
        """Return the entity icon."""
        if self.model.state == 'idle':
            return 'mdi:circle-outline'
        if self.model.state == 'active':
            return 'mdi:check-circle'
        if self.model.state == 'active_timer':
            return 'mdi:timer'
        if self.model.state == 'constrained':
            return 'mdi:cancel'
        if self.model.state == 'overridden':
            return 'mdi:timer-off'
        if self.model.state == 'blocked':
            return 'mdi:close-circle'
        return 'mdi:eye'

    @property
    def state_attributes(self):
        """Return the state of the entity."""
        return self.attributes.copy()

    def reset_state(self):
        """ Reset state attributes by removing any state specific attributes when returning to idle state """
        self.model.log.debug("Resetting state")
        att = {}

        PERSISTED_STATE_ATTRIBUTES = [
            'last_triggered_by',
            'last_triggered_at',
            'state_entities',
            'control_entities',
            'sensor_entities',
            'override_entities',
            'delay',
            'sensor_type',
            'mode',
            'start_time',
            'end_time'
        ]
        for k, v in self.attributes.items():
            if k in PERSISTED_STATE_ATTRIBUTES:
                att[k] = v

        self.attributes = att
        self.do_update()

    def do_update(self, wait=False, **kwargs):
        """ Schedules an entity state update with HASS """
        # _LOGGER.debug("Scheduled update with HASS")
        if self.may_update:
            self.async_schedule_update_ha_state(True)

    def set_attr(self, k, v):
        if k == 'delay':
            v = str(v) + 's'
        self.attributes[k] = v

    # HA Callbacks
    async def async_added_to_hass(self):
        """Register update dispatcher."""
        self.may_update = True


class Model():
    """ Represents the transitions state machine model """

    def __init__(self, hass, config, machine, entity):
        self.hass = hass  # backwards reference to hass object
        self.entity = entity  # backwards reference to entity containing this model
        self.debug_day_length = config.get("day_length", None)
        self.stateEntities = []
        self.controlEntities = []
        self.sensorEntities = []
        self.offEntities = []
        self.timer_handle = None
        self.block_timer_handle = None
        self.sensor_type = None
        self.night_mode = None
        self.backoff = False
        self.backoff_count = 0
        self.light_params_day = {}
        self.light_params_night = {}
        self.lightParams = {}
        self.name = None
        self.stay = False
        self.start = None
        self.end = None
        self.reset_count = None
        self.log = logging.getLogger(__name__ + '.' + config.get('name'))
        self.log.setLevel(logging.DEBUG)
        self.log.debug(
            "Initialising EntityController entity with this configuration: " + str(
                config))
        self.name = config.get('name', 'Unnamed Entity Controller')
        self.log.debug("Controller name: " + str(self.name))

        machine.add_model(
            self)  # add here because machine generated methods are being used in methods below.
        self.config_static_strings(config)
        self.config_control_entities(config)
        self.config_state_entities(
            config)  # must come after config_control_entities (uses control entities if not set)
        self.config_sensor_entities(config)
        self.config_override_entities(config)
        self.config_off_entities(config)
        self.config_normal_mode(config)
        self.config_night_mode(
            config)  # must come after normal_mode (uses normal mode parameters if not set)
        self.config_times(config)
        self.config_other(config)
        self.prepare_service_data()
        # def draw(self):
        #     self.update()
        #     if self.do_draw:
        #         self.log.debug("Updating graph in state: " + self.state)
        #         self.get_graph().draw(self.image_path + self.image_prefix + str(self.name)+'.png', prog='dot', format='png')

    def update(self, wait=False, **kwargs):
        """ Called from different methods to report a state attribute change """
        # self.log.debug("Update called with {}".format(str(kwargs)))
        for k, v in kwargs.items():
            if v is not None:
                self.entity.set_attr(k, v)

        if wait == False:
            self.entity.do_update()

    def finalize(self):
        self.entity.do_update()

    # =====================================================
    # S T A T E   C H A N G E   C A L L B A C K S
    # =====================================================

    def sensor_state_change(self, entity, old, new):
        """ State change callback for sensor entities """
        self.log.debug("Sensor state change: " + new.state)
        self.log.debug("state: " + self.state)

        if self.matches(new.state, self.SENSOR_ON_STATE) and (
                self.is_idle() or self.is_active_timer() or self.is_blocked()):
            self.update(last_triggered_by=entity)
            self.sensor_on()

        if self.matches(new.state,
                        self.SENSOR_OFF_STATE) and self.is_duration_sensor() and self.is_active_timer():
            self.update(last_triggered_by=entity,
                        sensor_turned_off_at=datetime.now())
            # We only care about sensor off state changes when the sensor is a duration sensor and we are in active_timer state.
            self.sensor_off_duration()

    def override_state_change(self, entity, old, new):
        """ State change callback for override entities """
        self.log.debug("Override state change")
        if self.matches(new.state, self.OVERRIDE_ON_STATE) and (
                self.is_active() or self.is_active_timer() or self.is_idle() or self.is_blocked()):
            self.update(overridden_by=entity)
            self.override()
            self.update(overridden_at=str(datetime.now()))
        if self.matches(new.state,
                        self.OVERRIDE_OFF_STATE) and self.is_override_state_off() and self.is_overridden():
            self.enable()

    def state_entity_state_change(self, entity, old, new):
        """ State change callback for state entities """
        if self.is_active_timer():
            self.control()

        if self.is_blocked() and self.is_state_entities_off():
            self.enable()

    def _start_timer(self):
        self.log.info(self.lightParams)
        if self.backoff_count == 0:
            self.previous_delay = self.lightParams.get('delay', DEFAULT_DELAY)
        else:
            self.log.debug(
                "Backoff: %s,  count: %s, delay%s, factor: %s",
                self.backoff, self.backoff_count,
                self.lightParams.get('delay', DEFAULT_DELAY),
                self.backoff_factor)
            self.previous_delay = round(
                self.previous_delay * self.backoff_factor, 2)
            if self.previous_delay > self.backoff_max:
                self.log.debug(
                    "Max backoff reached. Will not increase further.")
                self.previous_delay = self.backoff_max
            self.update(delay=self.previous_delay)

        expiry_time = datetime.now() + timedelta(seconds=self.previous_delay)

        # not able to use async_call_later because no known way to check whether timer is active.
        # self.timer_handle = event.async_call_later(self.hass, self.previous_delay, self.timer_expire)
        # self.log.debug("Timer handle" + str(dir(self.timer_handle)))
        self.timer_handle = Timer(self.previous_delay, self.timer_expire)
        # self.log.debug("Delay: " + str(self.previous_delay))
        self.timer_handle.start()
        self.update(expires_at=expiry_time)

    def _cancel_timer(self):
        if self.timer_handle.is_alive():
            self.timer_handle.cancel()

    def _reset_timer(self):
        self.log.debug("Resetting timer" + str(self.backoff))
        self._cancel_timer()
        self.update(reset_at=datetime.now())
        if self.backoff:
            self.log.debug("inc backoff")
            self.backoff_count += 1
            self.update(backoff_count=self.backoff_count)
        self._start_timer()

        return True

    def timer_expire(self):
        # self.log.debug("Timer expired")
        if self.is_duration_sensor() and self.is_sensor_on():  # Ignore timer expiry because duration sensor overwrites timer
            self.update(expires_at="pending sensor")
        else:
            self.timer_expires()

    def block_timer_expire(self):
        self.log.debug("Blocked Timer expired - Turn off all control entities.")
        self.turn_off_control_entities()
        self.block_timer_expires()

    # =====================================================
    # S T A T E   M A C H I N E   C O N D I T I O N S
    # =====================================================
    def _override_entity_state(self):
        for e in self.overrideEntities:
            s = self.hass.states.get(e)
            if self.matches(s.state, self.OVERRIDE_ON_STATE):
                self.log.debug("Override entities are ON. [%s]", e)
                return e
        self.log.debug("Override entities are OFF.")
        return None

    def is_override_state_off(self):
        return self._override_entity_state() is None

    def is_override_state_on(self):
        return self._override_entity_state() is not None

    def _sensor_entity_state(self):
        for e in self.sensorEntities:
            s = self.hass.states.get(e)
            if self.matches(s.state, self.SENSOR_ON_STATE):
                self.log.debug("Sensor entities are ON. [%s]", e)
                return e
        self.log.debug("Sensor entities are OFF.")
        return None

    def is_sensor_off(self):
        return self._sensor_entity_state() is None

    def is_sensor_on(self):
        return self._sensor_entity_state() is not None

    def _state_entity_state(self):
        for e in self.stateEntities:
            s = self.hass.states.get(e)
            self.log.info(s)
            if self.matches(s.state, self.STATE_ON_STATE):
                self.log.debug("State entities are ON. [%s]", e)
                return e
        self.log.debug("State entities are OFF.")
        return None

    def is_state_entities_off(self):
        return self._state_entity_state() is None

    def is_state_entities_on(self):
        return self._state_entity_state() is not None

    def will_stay_on(self):
        return self.stay

    def is_night(self):
        if self.night_mode is None:
            return False  # if night mode is undefined, it's never night :)
        else:
            self.log.debug("NIGHT MODE ENABLED: " + str(self.night_mode))
            return self.now_is_between(self.night_mode['start_time'],
                                       self.night_mode['end_time'])

    def is_event_sensor(self):
        return self.sensor_type == SENSOR_TYPE_EVENT

    def is_duration_sensor(self):
        return self.sensor_type == SENSOR_TYPE_DURATION

    def is_timer_expired(self):
        expired = self.timer_handle.is_alive() == False
        self.log.debug("is_timer_expired -> " + str(expired))
        return expired

    # =====================================================
    # S T A T E   M A C H I N E   C A L L B A C K S
    # =====================================================
    def on_enter_idle(self):
        self.log.debug("Entering idle")
        self.entity.reset_state()

    def on_exit_idle(self):
        self.log.debug("Exiting idle")

    def on_enter_overridden(self):
        self.log.debug("Now overridden")

    def on_enter_active(self):
        self.update(last_triggered_at=str(datetime.now()))
        self.backoff_count = 0
        self.prepare_service_data()

        self._start_timer()

        self.log.debug(
            "light params before turning on: " + str(self.lightParams))
        for e in self.controlEntities:

            # self.log.debug("brightness value" + str(self.lightParams.get('brightness')))
            if self.lightParams.get('service_data') is not None:
                self.log.debug(
                    "Turning on %s with service parameters %s", e,
                    self.lightParams.get(
                        'service_data'))
                self.call_service(e, 'turn_on',
                                  **self.lightParams.get('service_data'))
            else:
                self.log.debug(
                    "Turning on %s (no parameters passed to service call)",
                    e)
                self.call_service(e, 'turn_on')
        self.enter()

    def on_exit_active(self):
        self.log.debug("Turning off entities, cancelling timer")
        self._cancel_timer()  # cancel previous timer
        self.update(delay=self.lightParams.get(
            'delay'))  # no need to update immediately
        self.turn_off_control_entities()

    def on_enter_blocked(self):
        self.update(blocked_at=datetime.now())
        self.update(blocked_by=self._state_entity_state())

        if self.block_timeout:
            self.block_timer_handle = Timer(self.block_timeout, self.block_timer_expire)
            self.block_timer_handle.start()
            self.update(block_timeout=self.block_timeout)

    def on_exit_blocked(self):
        if self.block_timer_handle.is_alive():
            self.block_timer_handle.cancel()
    # =====================================================
    #    C O N F I G U R A T I O N  &  V A L I D A T I O N
    # =====================================================

    def config_control_entities(self, config):

        self.controlEntities = []

        self.add(self.controlEntities, config, "entity")
        self.add(self.controlEntities, config, "entities")
        self.add(self.controlEntities, config, "entity_on")

        self.log.debug("Control Entities: " + str(self.controlEntities))

    def config_state_entities(self, config):
        self.stateEntities = []
        if config.get('state_entities', False):
            self.stateEntities.extend(config.get('state_entities', []))
            self.log.info("State Entities (explicitly defined): " + str(
                self.stateEntities))
            event.async_track_state_change(self.hass, self.stateEntities,
                                           self.state_entity_state_change)

        # If no state entities are defined, use control entites as state
        if len(self.stateEntities) == 0:
            self.stateEntities = self.controlEntities.copy()
            self.log.debug("Added Control Entities as state entities: " + str(
                self.stateEntities))
            event.async_track_state_change(self.hass, self.stateEntities,
                                           self.state_entity_state_change)

    def config_off_entities(self, config):

        self.offEntities = []
        if self.add(self.offEntities, config, "entity_off"):
            self.log.info('Off Entities: ' + str(self.offEntities))

    def config_sensor_entities(self, config):
        self.sensorEntities = []
        self.add(self.sensorEntities, config, 'sensor')
        self.add(self.sensorEntities, config, 'sensors')

        if len(self.sensorEntities) == 0:
            self.log.error(
                "No sensor entities defined. You must define at least one sensor entity.")

        self.log.debug("Sensor Entities: " + str(self.sensorEntities))

        event.async_track_state_change(self.hass, self.sensorEntities,
                                       self.sensor_state_change)

    def config_static_strings(self, config):
        DEFAULT_ON = ["on", "playing", "home"]
        DEFAULT_OFF = ["off", "idle", "paused", "away"]
        self.CONTROL_ON_STATE = config.get("control_states_on", DEFAULT_ON)
        self.CONTROL_OFF_STATE = config.get("control_states_off", DEFAULT_OFF)
        self.SENSOR_ON_STATE = config.get("sensor_states_on", DEFAULT_ON)
        self.SENSOR_OFF_STATE = config.get("sensor_states_off", DEFAULT_OFF)
        self.OVERRIDE_ON_STATE = config.get("override_states_on", DEFAULT_ON)
        self.OVERRIDE_OFF_STATE = config.get("override_states_off",
                                             DEFAULT_OFF)
        self.STATE_ON_STATE = config.get("state_states_on", DEFAULT_ON)
        self.STATE_OFF_STATE = config.get("state_states_off", DEFAULT_OFF)

        on = config.get('state_strings_on', False)
        if on:
            self.CONTROL_ON_STATE.extend(on)
            self.CONTROL_ON_STATE.extend(on)
            self.SENSOR_ON_STATE.extend(on)
            self.OVERRIDE_ON_STATE.extend(on)
            self.STATE_ON_STATE.extend(on)

        off = config.get('state_strings_off', False)
        if off:
            self.CONTROL_OFF_STATE.extend(off)
            self.SENSOR_OFF_STATE.extend(off)
            self.OVERRIDE_OFF_STATE.extend(off)
            self.STATE_OFF_STATE.extend(off)

    def config_night_mode(self, config):
        """
            Configured night mode parameters. If no night_mode service 
            parameters are given, the day mode parameters are used instead. 
            If those do not exist, the 
        """
        if "night_mode" in config:
            self.night_mode = config["night_mode"]
            night_mode = config["night_mode"]
            self.light_params_night['delay'] = night_mode.get('delay',
                                                              config.get(
                                                                  "delay",
                                                                  DEFAULT_DELAY))
            self.light_params_night['service_data'] = night_mode.get(
                'service_data', self.light_params_day.get('service_data'))

            if not "start_time" in night_mode:
                self.log.error("Night mode requires a start_time parameter !")

            if not "end_time" in night_mode:
                self.log.error("Night mode requires a end_time parameter !")

    def config_normal_mode(self, config):
        params = {}
        params['delay'] = config.get("delay", DEFAULT_DELAY)
        params['service_data'] = config.get("service_data", None)
        self.log.info("serivce data set up: " + str(config))
        self.light_params_day = params

    @property
    def start_time(self):
        """ Wrapper for _start_time_private """
        return self.debug_time_wrapper(self._start_time_private)

    @property
    def end_time(self):
        """ Wrapper for _end_time_private """
        return self.debug_time_wrapper(self._end_time_private)

    def config_times(self, config):
        if CONFIG_START_TIME in config and CONFIG_END_TIME in config:
            # FOR OPTIONAL DEBUGGING: for initial setup use the raw input value
            self._start_time_private = config.get(CONFIG_START_TIME)
            self._end_time_private = config.get(CONFIG_END_TIME)
            self.log.debug("DEbugging start ==========================================")
            self.dump_sun()
            start_time_parsed = self.parse_time(self.start_time)
            self.log.debug("start_time_parsed: %s",
                           start_time_parsed)

            self.log.debug("futurize outputs %s", self.futurize(start_time_parsed))

            self.log.debug("DEbugging end ==========================================")
            parsed_start = self.parse_time(self.start_time, aware=False)
            parsed_end = self.parse_time(self.end_time, aware=False)
            # parsed_start = datetime.now() + timedelta(seconds=5)
            # parsed_end = datetime.now() + timedelta(seconds=10)
            # FOR OPTIONAL DEBUGGING: subsequently use normal delay
            sparts = re.search(
                '^(now\s*[+-]\s*\d+)', config.get(CONFIG_START_TIME))
            if sparts is not None:
                self._start_time_private = sparts.group(1)
            eparts = re.search(
                '^(now\s*[+-]\s*\d+)', config.get(CONFIG_END_TIME))
            if eparts is not None:
                self._end_time_private = eparts.group(1)

            self.update(start=self.start_time)
            self.update(end=self.end_time)

            # parsed_start, parsed_end = self.adjust_times(parsed_start,
                        #                                              parsed_end)

            parsed_start = self.futurize(parsed_start)
            parsed_end = self.futurize(parsed_end)
            self.log.debug("Setting FIRST START callback for %s", parsed_start)
            self.log.debug("Setting FIRST END callback for %s", parsed_end)

            self.start_time_event_hook = event.async_track_point_in_time(
                self.hass, self.start_time_callback, parsed_start)
            self.end_time_event_hook = event.async_track_point_in_time(
                self.hass, self.end_time_callback, parsed_end)

            if not self.now_is_between(self.start_time, self.end_time):
                self.log.debug(
                    "Constrain period active. Scheduling transition to 'constrained'")
                event.async_call_later(self.hass, 1, self.constrain_entity)

    def config_override_entities(self, config):
        self.overrideEntities = []
        self.add(self.overrideEntities, config, 'override')
        self.add(self.overrideEntities, config, 'overrides')

        if len(self.overrideEntities) > 0:
            self.log.debug("Override Entities: " + str(self.overrideEntities))
            event.async_track_state_change(self.hass, self.overrideEntities,
                                           self.override_state_change)

    def config_other(self, config):
        self.log.debug("Config other")

        self.do_draw = config.get("draw", False)

        if "entity_off" in config:
            self.entityOff = config.get("entity_off", None)

        self.block_timeout = config.get("block_timeout", None)
        self.image_prefix = config.get('image_prefix', '/fsm_diagram_')
        self.image_path = config.get('image_path', '/conf/temp')
        self.backoff = config.get('backoff', False)
        self.stay = config.get('stay', False)

        if self.backoff:
            self.log.debug(
                "setting up backoff. Using delay as initial backoff value.")
            self.backoff_factor = config.get('backoff_factor', 1.1)
            self.backoff_max = config.get('backoff_max', 300)

        self.stay = config.get("stay", False)

        if config.get("sensor_type_duration"):
            self.sensor_type = SENSOR_TYPE_DURATION
        else:
            self.sensor_type = SENSOR_TYPE_EVENT

        self.update(sensor_type=self.sensor_type)

    # =====================================================
    #    E V E N T   C A L L B A C K S
    # =====================================================

    def constrain_entity(self, evt):
        """
            Event callback used on component setup if current time requires entity to start in constrained state.
        """
        self.constrain()

    def end_time_callback(self, evt):
        """
            Called when `end_time` is reached, will change state to `constrained` and schedule `start_time` callback.
        """
        self.log.debug("END TIME CALLBACK.")
        # must be reparsed to get up to date sunset/sunrise times
        # if self.debug_day_length:
        #     x = self.make_naive(dt.now() + timedelta(seconds=int(self.debug_day_length)))
        #     self.log.debug("using debug day lengh %s", x)
        #
        # else:
        x = self.parse_time(self.end_time)



        parsed_end = self.futurize(x)
        self.log.debug("END TIME CALLBACK. New callback set to %s (future)", parsed_end)
        self.end_time_event_hook = event.async_track_point_in_time(
            self.hass, self.end_time_callback, parsed_end)
        self.update(end_time=parsed_end)
        # must be down here to make sure new callback is set regardless of exceptions
        self.constrain()

    def start_time_callback(self, evt):
        """

            Called when `start_time` is reached, will change state to `idle` and schedule `end_time` callback.
        """
        self.log.debug("START TIME CALLBACK.")
        # must be reparsed to get up to date sunset/sunrise times
        # if self.debug_day_length:
        #     x = self.make_naive(dt.now() + timedelta(seconds=int(self.debug_day_length)))
        #     self.log.debug("using debug day lengh %s", x)
        # else:
        x = self.parse_time(self.start_time)

        parsed_start = self.futurize(x)
        self.log.debug("START TIME CALLBACK."
                       " New callback set to %s (future)", parsed_start)
        self.start_time_event_hook = event.async_track_point_in_time(
            self.hass, self.start_time_callback, parsed_start)

        self.update(start_time=parsed_start)
        self.enable()
    # =====================================================
    #    H E L P E R   F U N C T I O N S        ( N E W )
    # =====================================================
    def turn_off_control_entities(self):
        if len(self.offEntities) > 0:
            self.log.info(
                "Turning on special off_entities that were defined, "
                "instead of turning off the regular control_entities")
            for e in self.offEntities:
                self.log.debug("Turning on %s", e)
                self.call_service(e, 'turn_on')
        else:
            for e in self.controlEntities:
                self.log.debug("Turning off %s", e)
                self.call_service(e, 'turn_off')

    def now_is_between(self, start_time_str, end_time_str, name=None):
        start_time = (self._parse_time(start_time_str, name))["datetime"]
        end_time = (self._parse_time(end_time_str, name))["datetime"]
        now = dt.as_local(dt.now())
        start_date = now.replace(
            hour=start_time.hour, minute=start_time.minute,
            second=start_time.second
        )
        end_date = now.replace(
            hour=end_time.hour, minute=end_time.minute, second=end_time.second
        )
        if end_date < start_date:
            # Spans midnight
            if now < start_date and now < end_date:
                now = now + timedelta(days=1)
            end_date = end_date + timedelta(days=1)

        self.log.debug("now_is_between start time %s", start_date)
        self.log.debug("now_is_between end time %s", end_date)
        return start_date <= now <= end_date

    def parse_time(self, time_str, name=None, aware=False):
        if aware is True:
            return dt.as_local(self._parse_time(time_str, name)[
                                   "datetime"]).time()
        else:
            return self.make_naive(
                (self._parse_time(time_str, name))["datetime"]).time()

    def parse_datetime(self, time_str, name=None, aware=False):
        if aware is True:
            return dt.as_local(self._parse_time(time_str, name)[
                                   "datetime"])
        else:
            return self.make_naive(dt.as_local(
                self._parse_time(time_str, name)["datetime"]))

    def _parse_time(self, time_str, name=None):
        parsed_time = None
        sun = None
        offset = 0
        parts = re.search('^(\d+)-(\d+)-(\d+)\s+(\d+):(\d+):(\d+)$',
                          str(time_str))
        if parts:
            this_time = datetime(int(parts.group(1)),
                                 int(parts.group(2)),
                                 int(parts.group(3)),
                                 int(parts.group(4)),
                                 int(parts.group(5)),
                                 int(parts.group(6)), 0)
            parsed_time = dt.as_local(this_time)
        else:
            parts = re.search('^(\d+):(\d+):(\d+)$', str(time_str))
            if parts:
                today = dt.as_local(dt.now())
                time_temp = time(
                    int(parts.group(1)), int(parts.group(2)),
                    int(parts.group(3)), 0
                )
                parsed_time = today.replace(hour=time_temp.hour,
                                            minute=time_temp.minute,
                                            second=time_temp.second,
                                            microsecond=0)

            else:
                if time_str == "sunrise":
                    parsed_time = self.sunrise(True)
                    sun = "sunrise"
                    offset = 0
                elif time_str == "sunset":
                    parsed_time = self.sunset(True)
                    sun = "sunset"
                    offset = 0
                else:
                    parts = re.search(
                        '^sunrise\s*([+-])\s*(\d+):(\d+):(\d+)$', str(time_str)
                    )
                    if parts:

                        sun = "sunrise"
                        if parts.group(1) == "+":
                            td = timedelta(
                                hours=int(parts.group(2)),
                                minutes=int(parts.group(3)),
                                seconds=int(parts.group(4))
                            )
                            offset = td.total_seconds()
                            parsed_time = (self.sunrise(True) + td)
                        else:
                            td = timedelta(
                                hours=int(parts.group(2)),
                                minutes=int(parts.group(3)),
                                seconds=int(parts.group(4))
                            )
                            offset = td.total_seconds() * -1
                            parsed_time = (self.sunrise(True) - td)
                    else:
                        parts = re.search(
                            '^sunset\s*([+-])\s*(\d+):(\d+):(\d+)$',
                            str(time_str)
                        )
                        if parts:
                            sun = "sunset"
                            if parts.group(1) == "+":
                                td = timedelta(
                                    hours=int(parts.group(2)),
                                    minutes=int(parts.group(3)),
                                    seconds=int(parts.group(4))
                                )
                                offset = td.total_seconds()
                                parsed_time = (self.sunset(True) + td)
                            else:
                                td = timedelta(
                                    hours=int(parts.group(2)),
                                    minutes=int(parts.group(3)),
                                    seconds=int(parts.group(4))
                                )
                                offset = td.total_seconds() * -1
                                parsed_time = (self.sunset(True) - td)
        if parsed_time is None:
            if name is not None:
                raise ValueError(
                    "%s: invalid time string: %s", name, time_str)
            else:
                raise ValueError("invalid time string: %s", time_str)
        # self.log.debug("Result of parsing: %s",
        #                {"datetime": parsed_time, "sun": sun, "offset": offset})
        return {"datetime": parsed_time, "sun": sun, "offset": offset}

    def make_naive(self, dts):
        local = dt.as_local(dts)
        return datetime(local.year, local.month, local.day,
                        local.hour, local.minute, local.second,
                        local.microsecond)

    def sunset(self, aware):
        t = get_astral_event_date(self.hass,
                                  SUN_EVENT_SUNSET,
                                  datetime.now().replace(hour=0))
        if aware is True:
            return dt.as_local(t)
        else:
            return t


    def sunrise(self, aware):
        t = get_astral_event_date(self.hass,
                                  SUN_EVENT_SUNRISE,
                                  datetime.now().replace(hour=0))
        if aware is True:
            return dt.as_local(t)
        else:
            return t

    def next_sunrise(self, offset=0):
        mod = offset
        while True:

            next_rising_dt = self.sunrise(True) + timedelta(mod)
            if next_rising_dt > dt.now():
                break

            mod += 1

        return next_rising_dt


    def next_sunset(self, offset=0):
        mod = offset
        while True:

            next_setting_dt = self.sunset(True) + timedelta(mod)
            if next_setting_dt > dt.now():
                break

            mod += 1

        return next_setting_dt


    # =====================================================
    #    H E L P E R   F U N C T I O N S    ( C U S T O M )
    # =====================================================

    # def adjust_times(self, start, end):
    #     """ Makes sure that a time period is in the future. """
    #     self.log.debug("Parsed start time (unadjusted) %s", start)
    #     self.log.debug("Parsed end time (unadjusted) %s", end)
    #
    #     # set start_time callback: if time passed, use tomorrow
    #     # --------s--------n----|12am|--s(new)--------
    #     #         \_________>>>________/
    #     now = datetime.now()
    #     if start <= now:
    #         start += timedelta(1)  # start time is tomorrow!
    #
    #     # we now need parsed_end to come after the new parse_start
    #     # (1) ---s---e---now
    #     # (2) ---e---s---now
    #     if end <= now:
    #         end += timedelta(1)  # (1)
    #     # if end <= start:
    #     # bump again because its still before s
    #     #    end += timedelta(1)  # (2)
    #     return start, end

    def prepare_service_data(self):
        """
            Called when entering active state and on initial set up to set
            correct service parameters.
        """
        if self.is_night():
            self.log.debug(
                "Using NIGHT MODE parameters: " + str(self.light_params_night))
            self.lightParams = self.light_params_night
            self.update(mode=MODE_NIGHT)
        else:
            self.log.debug(
                "Using DAY MODE parameters: " + str(self.light_params_day))
            self.lightParams = self.light_params_day
            if self.night_mode is not None:
                self.update(mode=MODE_DAY)  # only show when night mode set up
        self.update(delay=self.lightParams.get('delay'))

    def call_service(self, entity, service, **kwargs):
        """ Helper for calling HA services with the correct parameters """
        domain, e = entity.split('.')
        params = {}
        if kwargs is not None:
            params = kwargs

        params['entity_id'] = entity

        self.hass.services.call(domain, service, kwargs)
        self.update(service_data=kwargs)

    def matches(self, value, list):
        """
            Checks whether a string is contained in a list (used for matching state strings)
        """
        try:
            index = list.index(value)
            return True
        except ValueError:
            return False

    def five_seconds_from_now(self, sun):
        """ Returns a timedelta that will result in a sunrise trigger in 5 seconds time"""

        return dt.now() + timedelta(seconds=5) - get_astral_event_date(
            self.hass, sun, datetime.now())

    def five_minutes_ago(self, sun):
        """ Returns a timedelta that will result in a sunrise trigger in 5 seconds time"""
        return dt.now() - timedelta(minutes=5) - get_astral_event_date(
            self.hass, sun, datetime.now())

    def add(self, list, e, key=None):
        """ Adds e (which can be a string or list or config) to the list
            if e is defined.
        """
        if e is not None:
            v = []
            if key is not None:
                if key in e:  # must be in separate if statement
                    v = e[key]
            else:
                v = e

            if type(v) == str:
                list.append(v)
            else:
                list.extend(v)
        else:
            self.log.debug("none")
        return len(v) > 0

    def futurize(self, timet):
        """ Returns tomorrows time if time is in the past.
            Input time should be offset aware
         """

        self.log.debug("-------------------- futurize ------------------------")
        self.log.debug("Input (naive) %s ", timet)
        today = date.today()
        try:
            t = datetime.combine(today, timet)
        except TypeError as e:
            t = timet
        x = datetime.now()
        self.log.debug("input time: " + str(t))

        self.log.debug("current time: " + str(x))
        while t <= x:
            if t <= x:
                if self.debug_day_length is not None:
                    t = t + timedelta(seconds=int(self.debug_day_length) ) # tomorrow!
                else:
                    t = t + timedelta(1)  # tomorrow!
                self.log.debug( "Time already happened. Returning tomorrow instead. " + str(t))
            else:
                self.log.debug( "Time still happening today. " + str(t))
        self.log.debug("output time: %s", t)
        self.log.debug("-------------------- futurize (END) -------------------")
        return t

    def debug_time_wrapper(self, timet):
        """

            Injects some debugging capability. Number is parenthesis is the
            first delay used on initial component setup. (This creates a time
            difference between start and end time callbacks.)

            The other number after the + sign is the standard period.
            In real life, this would be 24 hours, for debugging you can make
            it a few seconds to see the app change from idle to constrained.

            start_time: now + 5 (3)
            end_time: now + 5 (6)

            This function is used to wrap CONFIG_START_TIME and CONFIG_END_TIME
            and should only be called by the corresponding class properties!

            See config_times.
        """
        s = timet
        parts = re.search(
            '^now\s*([+-])\s*(\d+)\s*\(?(\d+)?\)?$', timet
        )
        if parts:
            sign = parts.group(1)
            first_delay = parts.group(3)
            delay = parts.group(2)
            # self.log.debug("Group 1: %s", sign)
            # self.log.debug("Group 2 (delay): %s", delay)
            # self.log.debug("Group 3 (first delay): %s", first_delay)
            now = dt.now()
            self.log.debug("now %s", now)
            delta = timedelta(seconds=int(delay))
            if first_delay is not None:
                # self.log.debug("first delay defined")
                delta = timedelta(seconds=int(first_delay))
            if sign == '-':
                now = now - delta
            else:
                now = now + delta

            # self.log.debug("now + delta %s", now)

            s = str(self.make_naive(now).time().replace(microsecond=0))

        # self.log.debug("config time s %s", s)
        return s


    def dump_sun(self):
        self.log.debug("--------------------------------------------------")
        self.log.debug("Time Dump")
        self.log.debug("--------------------------------------------------")
        self.log.debug("Start time:             %s", self._start_time_private)
        self.log.debug("End time:               %s", self._end_time_private)
        self.log.debug("Start time (property):  %s", self.start_time)
        self.log.debug("End time (property):    %s", self.end_time)
        self.log.debug("DT Now:                 %s", dt.now())
        self.log.debug("datetime Now:           %s", datetime.now())
        self.log.debug("Next Sunrise:           %s", self.next_sunrise(True))
        self.log.debug("Next Sunset:            %s", self.next_sunset(True))
        self.log.debug("Sunrise:                %s", self.sunrise(True))
        self.log.debug("Sunset:                 %s", self.sunset(True))
        self.log.debug("--------------------------------------------------")
        self.log.debug("Sunset Diff (to now): %s",
                       self.next_sunset() - dt.now())
        self.log.debug("Sunrise Diff(to now): %s",
                       self.next_sunset() - dt.now())
        self.log.debug("--------------------------------------------------")
