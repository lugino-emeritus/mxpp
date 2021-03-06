import logging
from typing import Dict, Tuple, List

import sleekxmpp
import requests
import yaml

from matrix_client.client import MatrixClient
from matrix_client.errors import MatrixError
from matrix_client.room import Room as MatrixRoom
from mxpp.client_xmpp import ClientXMPP

CONFIG_FILE = 'config.yaml'

logging.basicConfig(level=logging.DEBUG,
                    format='%(levelname)-8s %(message)s')
logging.getLogger(sleekxmpp.__name__).setLevel(logging.ERROR)
logging.getLogger(requests.__name__).setLevel(logging.ERROR)


class BridgeBot:
    xmpp = None                        # type: ClientXMPP
    matrix = None                      # type: MatrixClient
    topic_room_id_map = None           # type: Dict[str, str]
    special_rooms = None               # type: Dict[str, MatrixRoom]
    special_room_names = None          # type: Dict[str, str]
    groupchat_flag = None              # type: str
    groupchat_jids = None              # type: List[str]
    restore_room_topic = True          # type: bool

    users_to_invite = None             # type: List[str]
    matrix_room_topics = None          # type: Dict[str, str]
    matrix_server = None               # type: Dict[str, str]
    matrix_login = None                # type: Dict[str, str]
    xmpp_server = None                 # type: Tuple[str, int]
    xmpp_login = None                  # type: Dict[str, str]
    xmpp_roster_options = None         # type: Dict[str, bool]
    xmpp_groupchat_nick = None         # type: str

    send_messages_to_all_chat = True   # type: bool
    disable_all_chat_room = False      # type: bool
    send_presences_to_control = True   # type: bool
    groupchat_mute_own_nick = True     # type: bool

    disabled_jids = set()             # type: Set[str]

    @property
    def bot_id(self) -> str:
        return self.matrix_login['username']

    def __init__(self, config_file: str=CONFIG_FILE):
        self.groupchat_jids = []
        self.topic_room_id_map = {}
        self.special_rooms = {
                'control': None,
                'all_chat': None,
                }
        self.special_room_names = {
                'control': 'XMPP Control Room',
                'all_chat': 'XMPP All Chat',
                }
        self.xmpp_roster_options = {}

        self.load_config(config_file)

        self.matrix = MatrixClient(**self.matrix_server)
        self.xmpp = ClientXMPP(**self.xmpp_login, **self.xmpp_roster_options)

        self.matrix.login_with_password(**self.matrix_login)

        if self.disable_all_chat_room:
            self.send_messages_to_all_chat = False #should not be necessary (see load_config)
            if 'all_chat' in self.special_rooms:
                del self.special_rooms['all_chat']
            if 'all_chat' in self.special_room_names:
                del self.special_room_names['all_chat']

        # Prepare matrix special channels and their listeners
        for room in self.matrix.get_rooms().values():
            room.update_room_topic()
            topic = room.topic

            if topic in self.special_rooms.keys():
                logging.debug('Recovering special room: ' + topic)
                self.special_rooms[topic] = room

            elif topic.startswith(self.groupchat_flag):
                room_jid = topic[len(self.groupchat_flag):]
                self.groupchat_jids.append(room_jid)

        for topic, room in self.special_rooms.items():
            if room is None:
                room = self.matrix.create_room()
            self.setup_special_room(room, topic)

        self.special_rooms['control'].add_listener(self.matrix_control_message, 'm.room.message')
        if not self.disable_all_chat_room:
            self.special_rooms['all_chat'].add_listener(self.matrix_all_chat_message, 'm.room.message')

        # Invite users to special rooms
        for room in self.special_rooms.values():
            for user_id in self.users_to_invite:
                room.invite_user(user_id)

        # Prepare xmpp listeners
        self.xmpp.add_event_handler('roster_update', self.xmpp_roster_update)
        self.xmpp.add_event_handler('message', self.xmpp_message)
        self.xmpp.add_event_handler('presence_available', self.xmpp_presence_available)
        self.xmpp.add_event_handler('presence_unavailable', self.xmpp_presence_unavailable)
        self.xmpp.add_event_handler('groupchat_message', self.xmpp_groupchat_message)

        # Connect to XMPP and start processing XMPP events
        self.xmpp.connect(self.xmpp_server)
        self.xmpp.process(block=False)

        # Rejoin group chats
        logging.debug('Rejoining group chats')
        for room_jid in self.groupchat_jids:
            self.xmpp.plugin['xep_0045'].joinMUC(room_jid, self.xmpp_groupchat_nick)

        logging.debug('Done with bot init')

    def load_config(self, path: str):
        with open(path, 'r') as conf_file:
            config = yaml.load(conf_file)

        self.users_to_invite = config['matrix']['users_to_invite']
        self.matrix_room_topics = config['matrix']['room_topics']
        self.groupchat_flag = config['matrix']['groupchat_flag']
        if 'restore_room_topic' in config['matrix']: # to be compatible to other config files
            self.restore_room_topic = config['matrix']['restore_room_topic']

        self.matrix_server = config['matrix']['server']
        self.matrix_login = config['matrix']['login']
        self.xmpp_server = (config['xmpp']['server']['host'],
                            config['xmpp']['server']['port'])
        self.xmpp_login = config['xmpp']['login']
        self.xmpp_groupchat_nick = config['xmpp']['groupchat_nick']

        self.send_presences_to_control = config['send_presences_to_control']
        self.send_messages_to_all_chat = config['send_messages_to_all_chat']
        if not self.send_messages_to_all_chat and 'disable_all_chat_room' in config:
            self.disable_all_chat_room = config['disable_all_chat_room']
        else:
            self.disable_all_chat_room = False

        self.groupchat_mute_own_nick = config['groupchat_mute_own_nick']

        self.xmpp_roster_options = config['xmpp']['roster_options']

        if 'disabled_jids' in config:
            self.disabled_jids = set(config['disabled_jids'])
            if 'xmpp_login_jid' in self.disabled_jids:
                self.disabled_jids.add(self.xmpp_login['jid'])
                self.disabled_jids.remove('xmpp_login_jid')

    def get_room_for_jid(self, jid: str) -> MatrixRoom:
        """
        Return the room corresponding to the given XMPP JID
        :param jid: bare XMPP JID, should not include the resource
        :return: Matrix room object for chatting with that JID
        """
        room_id = self.topic_room_id_map[jid]
        return self.matrix.get_rooms()[room_id]

    def get_unmapped_rooms(self) -> List[MatrixRoom]:
        """
        Returns a list of all Matrix rooms which are not a special room (e.g., the control room) and
        do not have a corresponding entry in the topic -> room map.
        :return: List of unmapped, non-special Matrix room objects.
        """
        special_room_ids = [r.room_id for r in self.special_rooms.values()]
        valid_room_ids = [v for v in self.topic_room_id_map.values()] + special_room_ids
        unmapped_rooms = [room for room_id, room in self.matrix.get_rooms().items()
                          if room_id not in valid_room_ids]
        return unmapped_rooms

    def get_empty_rooms(self) -> List[MatrixRoom]:
        """
        Returns a list of all Matrix rooms which are occupied by only one user
        (the bot itself).
        :return: List of Matrix rooms occupied by only the bot.
        """
        empty_rooms = [room for room in self.matrix.get_rooms().values()
                       if len(room.get_joined_members()) < 2]
        return empty_rooms

    def setup_special_room(self, room, topic: str):
        """
        Sets up a Matrix room with the requested topic and adds it to the self.special_rooms map.

        If a special room with that topic already exists, it is replaced in the special_rooms
         map by the new room.
        :param room: Room to set up
        :param topic: Topic for the room
        """
        room.set_room_topic(topic)
        room.set_room_name(self.special_room_names[topic])
        self.special_rooms[topic] = room

        logging.debug('Set up special room with topic {} and id'.format(
            str(room.topic), room.room_id))

    def create_mapped_room(self, topic: str, name: str=None) -> MatrixRoom or None:
        """
        Create a new room and add it to self.topic_room_id_map.

        :param topic: Topic for the new room
        :param name: (Optional) Name for the new room
        :return: Room which was created
        """
        if not name: #room without name is shown as the bot's name in clients like riot
            name = topic

        if topic in self.groupchat_jids:
            logging.debug('Topic {} is a groupchat without its flag, ignoring'.format(topic))
            return None
        elif topic in self.topic_room_id_map.keys():
            room_id = self.topic_room_id_map[topic]
            room = self.matrix.get_rooms()[room_id]
            logging.debug('Room with topic {} already exists!'.format(topic))
        else:
            room = self.matrix.create_room()
            room.set_room_topic(topic)
            self.topic_room_id_map[topic] = room
            logging.info('Created mapped room with topic {} and id {}'.format(topic, str(room.room_id)))
            room.set_room_name(name)

        room.update_room_name() #room.name is not set automatically in all cases

        if self.restore_room_topic and room.name != name:
            room.set_room_name(name)

        return room

    def map_rooms_by_topic(self):
        """
        Add unmapped rooms to self.topic_room_id_map, and listen to messages from those rooms.

        Rooms whose topics are empty or do not contain an '@' symbol are assumed to be special
         rooms, and will not be mapped.
        """
        unmapped_rooms = self.get_unmapped_rooms()

        for room in unmapped_rooms:
            room.update_room_topic()

            logging.debug('Unmapped room {} ({}) [{}]'.format(room.room_id, room.name, room.topic))

            if room.topic is None or '@' not in room.topic:
                logging.debug('Leaving it as-is (special room, topic does not contain @)')
            else:
                self.topic_room_id_map[room.topic] = room.room_id

                room.add_listener(self.matrix_message, 'm.room.message')

    def matrix_control_message(self, room: MatrixRoom, event: Dict):
        """
        Handle a message sent to the control room.

        Does nothing unless a valid command is received:
          refresh  Probes the presence of all XMPP contacts, and updates the roster.
          purge    Leaves any ((un-mapped and non-special) or empty) Matrix rooms.
          joinmuc some@muc.com   Joins a muc
          leavemuc some@muc.com  Leaves a muc

        :param room: Matrix room object representing the control room
        :param event: The Matrix event that was received. Assumed to be an m.room.message .
        """
        # Always ignore our own messages
        if event['sender'] == self.bot_id:
            return

        logging.debug('matrix_control_message: {}  {}'.format(room.room_id, str(event)))

        if event['content']['msgtype'] == 'm.text':
            message_body = event['content']['body']
            logging.info('Matrix received control message: ' + message_body)

            message_parts = message_body.split()
            if len(message_parts) > 0:
                message_parts[0] = message_parts[0].lower()
                # what about a empty body?
                if message_parts[0] == 'refresh':
                    for jid in self.topic_room_id_map.keys():
                        self.xmpp.send_presence(pto=jid, ptype='probe')

                    self.xmpp.send_presence()
                    self.xmpp.get_roster()

                elif message_parts[0] == 'purge':
                    self.special_rooms['control'].send_text('Purging unused rooms')

                    # Leave from unwanted rooms
                    for room in self.get_unmapped_rooms() + self.get_empty_rooms():
                        logging.info('Leaving room {r.room_id} ({r.name}) [{r.topic}]'.format(r=room))
                        if room.topic.startswith(self.groupchat_flag):
                            room_jid = room.topic[len(self.groupchat_flag):]
                            self.xmpp.plugin['xep_0045'].leaveMUC(room_jid)
                        room.leave()

                elif len(message_parts) > 1:
                    if message_parts[0] == 'joinmuc':
                        room_jid = message_parts[1]
                        logging.info('XMPP MUC join: {}'.format(room_jid))
                        self.create_groupchat_room(room_jid)
                        self.xmpp.plugin['xep_0045'].joinMUC(room_jid, self.xmpp_groupchat_nick)
                    elif message_parts[0] == 'leavemuc':
                        room_jid = message_parts[1]
                        logging.info('XMPP MUC leave: {}'.format(room_jid))
                        self.xmpp.plugin['xep_0045'].leaveMUC(room_jid, self.xmpp_groupchat_nick)
                        room = self.get_room_for_jid(self.groupchat_flag + room_jid)
                        room.leave()

    def matrix_all_chat_message(self, room: MatrixRoom, event: Dict):
        """
        Handle a message sent to Matrix all-chat room.

        Currently just sends a warning that nobody will hear your message.

        :param room: Matrix room object representing the all-chat room
        :param event: The Matrix event that was received. Assumed to be an m.room.message .
        """
        # Always ignore our own messages
        if event['sender'] == self.bot_id:
            return

        logging.debug('matrix_all_chat_message: {}  {}'.format(room.room_id, str(event)))

        room.send_notice('Don\'t talk in here! Nobody gets your messages.')

    def matrix_message(self, room: MatrixRoom, event: Dict):
        """
        Handle a message sent to a mapped Matrix room.

        Sends the message to the xmpp handle specified by the room's topic.

        :param room: Matrix room object representing the room in which the message was received.
        :param event: The Matrix event that was received. Assumed to be an m.room.message .
        """
        if event['sender'] == self.bot_id:
            return

        if room.topic in self.special_rooms.keys():
            logging.error('matrix_message called on special channel')

        logging.debug('matrix_message: {}  {}'.format(room.room_id, event))

        if event['content']['msgtype'] == 'm.text':
            message_body = event['content']['body']

            if room.topic.startswith(self.groupchat_flag):
                jid = room.topic[len(self.groupchat_flag):]
                message_type = 'groupchat'
            else:
                jid = room.topic
                message_type = 'chat'

            name = self.xmpp.jid_nick_map[jid]

            logging.info('Matrix received message to {} : {}'.format(jid, message_body))
            self.xmpp.send_message(mto=jid, mbody=message_body, mtype=message_type)

            if self.send_messages_to_all_chat:
                self.special_rooms['all_chat'].send_notice('To {} : {}'.format(name, message_body))

    def xmpp_message(self, message: Dict):
        """
        Handle a message received by the XMPP client.

        Sends the message to the relevant mapped Matrix room, as well as the Matrix all-chat room.

        :param message: The message that was received.
        :return:
        """
        logging.info('XMPP received {} : {}'.format(message['from'].full, message['body']))

        if message['type'] in ('normal', 'chat'):
            from_jid = message['from'].bare
            from_name = self.xmpp.jid_nick_map[from_jid]

            if from_jid in self.groupchat_jids:
                logging.warning('Normal chat message from a groupchat, ignoring...')
                return

            room = self.get_room_for_jid(from_jid)
            room.send_text(message['body'])
            if self.send_messages_to_all_chat:
                self.special_rooms['all_chat'].send_text('From {}: {}'.format(from_name, message['body']))

    def xmpp_groupchat_message(self, message: Dict):
        """
        Handle a groupchat message received by the XMPP client.

        Sends the message to the relevant mapped Matrix room, as well as the Matrix all-chat room.

        :param message: The message that was received.
        :return:
        """
        logging.info('XMPP MUC received {} : {}'.format(message['from'].full, message['body']))

        if message['type'] == 'groupchat':
            from_jid = message['from'].bare
            from_name = message['mucnick']

            if self.groupchat_mute_own_nick and from_name == self.xmpp_groupchat_nick:
                return

            room = self.get_room_for_jid(self.groupchat_flag + from_jid)
            room.send_text(from_name + ': ' + message['body'])
            if self.send_messages_to_all_chat:
                self.special_rooms['all_chat'].send_text(
                    'Room {}, from {}: {}'.format(from_jid, from_name, message['body']))

    def create_groupchat_room(self, room_jid: str):
        room = self.create_mapped_room(topic=self.groupchat_flag + room_jid)
        if room_jid not in self.groupchat_jids:
            self.groupchat_jids.append(room_jid)
        for user_id in self.users_to_invite:
            room.invite_user(user_id)

    def xmpp_presence_available(self, presence: Dict):
        """
        Handle a presence of type "available".

        Sends a notice to the control channel.

        :param presence: The presence that was received.
        """
        logging.debug('XMPP received {} : (available)'.format(presence['from'].full))

        jid = presence['from'].bare
        if jid in self.disabled_jids:
            return

        if jid not in self.xmpp.jid_nick_map.keys():
            logging.error('JID NOT IN ROSTER!?')
            self.xmpp.get_roster()
            return

        if self.send_presences_to_control:
            name = self.xmpp.jid_nick_map[jid]
            self.special_rooms['control'].send_notice('{} available ({})'.format(name, jid))

    def xmpp_presence_unavailable(self, presence):
        """
        Handle a presence of type "unavailable".

        Sends a notice to the control channel.

        :param presence: The presence that was received.
        """
        logging.debug('XMPP received {} : (unavailable)'.format(presence['from'].full))

        jid = presence['from'].bare
        if jid in self.disabled_jids:
            return

        if self.send_presences_to_control:
            name = self.xmpp.jid_nick_map[jid]
            self.special_rooms['control'].send_notice('{} unavailable ({})'.format(name, jid))

    def xmpp_roster_update(self, _event):
        """
        Handle an XMPP roster update.

        Maps all existing Matrix rooms, creates a new mapped room for each JID in the roster
        which doesn't have one yet, and invites the users specified in the config in to all the rooms.

        :param _event: The received roster update event (unused).
        """
        logging.debug('######### ROSTER UPDATE ###########')

        rjids = [jid for jid in self.xmpp.roster]
        if len(rjids) > 1:
            raise Exception('Not sure what to do with more than one roster...')

        roster0 = self.xmpp.roster[rjids[0]]
        self.xmpp.roster_dict = {jid: roster0[jid] for jid in roster0}
        roster = self.xmpp.roster_dict

        self.map_rooms_by_topic()

        # Create new rooms where none exist
        for jid, info in roster.items():
            if '@' not in jid:
                logging.warning('Skipping fake jid in roster: ' + jid)
                continue
            if jid in self.disabled_jids:
                continue
            name = info['name']
            self.xmpp.jid_nick_map[jid] = name
            self.create_mapped_room(topic=jid, name=name)

        logging.debug('Sending invitations..')
        # Invite to all rooms
        for room in self.matrix.get_rooms().values():
            users_in_room = room.get_joined_members()
            for user_id in self.users_to_invite:
                if user_id not in users_in_room:
                    room.invite_user(user_id)

        logging.debug('######## Done with roster update #######')


def main():
    while True:
        try:
            bot = BridgeBot()
            bot.matrix.listen_forever()
        except MatrixError as e:
            logging.error('MatrixError: {}'.format(e))
            pass


if __name__ == "__main__":
    main()
