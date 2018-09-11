import indigo

import socket
import telnetlib
import time
import datetime
import requests

GLOBAL_TIMEOUT = 3
IMAGE_PULL_UPDATE_FREQUENCY = 24
REST_WAIT_TIME = 4

class JAPDevice(object):
	def __init__(self, vlan, ip, firmware, logger, L2Debug):
		self.vlan = vlan
		self.ip = ip
		self.logger = logger
		self.L2Debug = L2Debug
		self.connection = None
		self.image_pull_url = "http://" + self.ip + "/pull.bmp"
		self._image_pull_enabled = None
		self.friendlyname = ip
		self.ignore = False
		self._quietConnectionAttempts = False
		self.firmware = firmware

	def _sendCommand(self, cmd):
		try:
			self._connect()
			self.logger.debug(u"Sending command:  %s" % cmd)
			cmdsend = cmd + "\r\n"
			self.connection.write(str(cmdsend))
			output = self.connection.read_until("#", GLOBAL_TIMEOUT)
			self.logger.debug(output)
			self.connection.write("exit")
			self.connection.close()
			self.connection = None

			return output
		except:
			self.logger.error("problem sending command")

	def ImagePullEnabled(self):
		if self.ignore:
			return False
		try:
			if self._image_pull_enabled is None or self._image_pull_refresh < datetime.datetime.now()-datetime.timedelta(hours=IMAGE_PULL_UPDATE_FREQUENCY):
				self.loadImagePull()
		except:
			self.loadImagePull()

		return self._image_pull_enabled

	def loadImagePull(self):
		if self.firmware == "A":
			output = self._sendCommand("astparam dump")

			if output is None:
				return False

			for astparam_output in output.splitlines():
				if "pull_on_boot" in astparam_output:
					self._image_pull_enabled = astparam_output[13] != "n"

					if self._image_pull_enabled:
						self.image_pull_res = astparam_output[13:].split("_")[0]
						self.image_pull_prior = astparam_output[13:].split("_")[1]
						self.image_pull_rate = astparam_output[13:].split("_")[2]

						if self.image_pull_prior == "1":
							self.image_pull_prior = "low"
						else:
							self.image_pull_prior = "high"
					else:
						self.image_pull_res = None
						self.image_pull_prior = None
						self.image_pull_rate = None

		elif self.firmware == "B":
			r = requests.get("http://" + self.ip + "/cgi-bin/api/settings/imagepull")

			if type(r.json()["data"]) is bool:
				self._image_pull_enabled = r.json()["data"] != False
			elif type(r.json()["data"]) is dict:
				self._image_pull_enabled = True

			if self._image_pull_enabled:
				self.image_pull_res = int(r.json()["data"]["width"])
				self.image_pull_prior = r.json()["data"]["priority"]
				self.image_pull_rate = int(r.json()["data"]["frequency"])

		if self._image_pull_enabled:
			indigo.server.log("loaded from device " + self.friendlyname + " that image pull is enabled (res: " + str(self.image_pull_res) + ", priority: " + str(self.image_pull_prior) + ", rate: " + str(self.image_pull_rate) + " secs)")
		else:
			indigo.server.log("loaded from device " + self.friendlyname + " that image pull is disabled")

		self._image_pull_refresh = datetime.datetime.now()

	def save(self):
		if self.firmware == "A":
			command = "astparam save"

			return self._sendCommand(command)

		elif self.firmware == "B":
			r = requests.post("http://" + self.ip + "/cgi-bin/api/command/device", data="save")
			indigo.server.log(self.friendlyname + ": save command sent.  response code: " + str(r.status_code) + ", response: " + r.text)

			return r.status_code == requests.codes.ok

		return False

	def enableImagePull(self, res=320, prior=1, rate=3):
		self.logger.debug("enabling image pull")

		if self.firmware == "A":
			command = "astparam s pull_on_boot " + str(res) + "_" + str(prior) + "_" + str(rate) + ";astparam save;reboot"

			self._sendCommand(command)

			self.image_pull_res = res
			self.image_pull_prior = prior
			self.image_pull_rate = rate
			self._image_pull_enabled = True
	
		elif self.firmware == "B":
			data = "{\"width\":\"320\",\"priority\":\"low\",\"frequency\":\"3\"}"	

			r = requests.post("http://" + self.ip + "/cgi-bin/api/settings/imagepull", data=data)
			indigo.server.log(self.friendlyname + ": enable image pull command sent.  response code: " + str(r.status_code) + ", response: " + r.text)

			self._image_pull_enabled = r.status_code == requests.codes.ok

			time.sleep(REST_WAIT_TIME)
			self.save()
			time.sleep(REST_WAIT_TIME)
			self.reboot()

		self._image_pull_refresh = datetime.datetime.now()

		return self._image_pull_enabled

	def disableImagePull(self):
		self.logger.debug("disabling image pull")

		if self.firmware == "A":

			command = "astparam s pull_on_boot n;astparam save;reboot"

			self._sendCommand(command)
			self._image_pull_enabled = False
			self._image_pull_refresh = datetime.datetime.now()		

			self.image_pull_res = None
			self.image_pull_prior = None
			self.image_pull_rate = None
			
		elif self.firmware == "B":
			data = "null"

			r = requests.post("http://" + self.ip + "/cgi-bin/api/settings/imagepull", data=data)

			indigo.server.log(self.friendlyname + ": disable image pull command sent.  response code: " + str(r.status_code) + ", response: " + r.text)
			self._image_pull_enabled = r.status_code == requests.codes.ok

			time.sleep(REST_WAIT_TIME)
			self.save()
			time.sleep(REST_WAIT_TIME)
			self.reboot()

		self._image_pull_refresh = datetime.datetime.now()
		return not self._image_pull_enabled		

	def reboot(self):
		command = "reboot"

		if self.firmware == "A":
			return self._sendCommand(command)
		elif self.firmware == "B":
			try:
				r = requests.post("http://" + self.ip + "/cgi-bin/api/command/device", data=command, timeout=1)
				indigo.server.log(self.friendlyname + ": reboot command sent.  response code: " + str(r.status_code) + ", response: " + r.text)
			except:
				return True

			return r.status_code == requests.codes.ok

		return False

	def _connect(self):
		try:
			self.logger.info(u"Connecting to " + self.friendlyname + " (via IP:  %s" % self.ip + ")")
			self.connection = telnetlib.Telnet(self.ip, 23)
			output = self.connection.read_until("#", GLOBAL_TIMEOUT)
			self.logger.debug(output)

			time.sleep(3)

		except:
			self.logger.exception("Connection attempt failed. %s" % e.message)
			time.sleep(5)

		self.logger.debug("Connected")

class JustAddPowerTransmitter(JAPDevice):
	def __init__(self, vlan, ip, firmware, logger, L2Debug):
		super(JustAddPowerTransmitter, self).__init__(vlan, ip, firmware, logger, L2Debug)

#		self.vlan = int(vlan)
#		self.ip = ip

		self.no = int(vlan) - 10
		self.ignore = False  # Set this to true for a device that is set up in JADConfig but isn't actually being used. e.g. when you make a larger matrix to give yourself headroom.
		self.being_watched = "Unknown"

class JustAddPowerReceiver(JAPDevice):
	def __init__(self, vlan, no, ip, port, firmware, logger, L2Debug):
		super(JustAddPowerReceiver, self).__init__(vlan, ip, firmware, logger, L2Debug)
#		self.vlan = int(vlan)
#		self.ip = ip

		self.no = int(no)
		self.ignore = False # Set this to true for a device that is set up in JADConfig but isn't actually being used. e.g. when you make a larger matrix to give yourself headroom.
		self.port = port
		self.vlan_watching = "Unknown"

class JustAddPowerMatrix(object):
	"""docstring for JustAddPowerMatrix"""
	def __init__(self, model, ip, login, password, controlVLAN = 2, firmware = "A", logger = None, L2Debug = False):
		super(JustAddPowerMatrix, self).__init__()
		self.model = model
		self.ip = ip
		self.login = login
		self.password = password
		self.RxCount = 0
		self.TxCount = 0
		self.controlVLAN = controlVLAN
		self.receiverVLAN = 10
		self.firmware = firmware

		self.Rx = []
		self.Tx = []

		self.logger = logger
		self.L2Debug = L2Debug

		self._connect()
		self._loadConfiguration()

	def allDevices(self):
		return self.Rx + self.Tx

	def watch(self, Rx, Tx):
		self.logger.debug("starting commands to change Rx VLAN")
		self._sendCommand("enable")
		self._sendCommand("config")

		command = "interface gi" + str(Rx.port)

		self._sendCommand(command)

		self._sendCommand("switchport general allowed vlan remove 11-410")

		command = "switchport general allowed vlan add " + str(Tx.vlan) + " untagged"

		self._sendCommand(command)
		self._sendCommand("end")

		self.connection.read_eager()

#		output = self.connection.read_until("#", GLOBAL_TIMEOUT)

#		if self.L2Debug:
#			self.logger.debug(output)

	def _addReceiver(self, newRx):
		self.Rx.append(newRx)
		self.RxCount = self.RxCount + 1

	def _addTransmitter(self, newTx):	
		self.Tx.append(newTx)
		self.TxCount = self.TxCount + 1

	def isConfigured(self):
		return len(self.ip) > 0 and len(self.password) > 0 and len(self.login) > 0

	def _reconnect(self):
		self.connection	= None
		self._connect()

	def _connect(self):

		if not self.isConfigured():
			return

		try:
			self.logger.info(u"Connecting to switch via IP to %s" % self.ip)
			self.connection = telnetlib.Telnet(self.ip, 23)
			time.sleep(3)

			a = self.connection.read_until("User Name:", GLOBAL_TIMEOUT)
			self.logger.debug(u"Telnet: %s" % a)

			if 'User' in a:
				self.logger.debug(u"Sending username.")
				self.connection.write(str(self.login) + "\r\n")

				a = self.connection.read_until("Password:", GLOBAL_TIMEOUT)
				self.logger.debug(u"Telnet: %s" % a)

				if 'Password' in a:
					self.logger.debug(u"Sending password.")
					self.connection.write(str(self.password) + "\r\n")

					a = self.connection.read_until("#", GLOBAL_TIMEOUT)
					self.logger.debug(u"Telnet: %s" % a)

				else:
					self.logger.debug(u"password failure.")
			else:
				self.logger.debug(u"username failure.")

			self.logger.info(u"Connected to switch")
			self.logger.debug(u"End of connection process.")

		except Exception as e:
			self.logger.debug(u"Unable to connect.  Error: %s" % e.message)

	def updatePorts(self):
		self.logger.debug("starting port refresh")

		for dev in self.Tx:
			dev.being_watched = "Not in use"

		for dev in self.Rx:
			dev.vlan_watching = "Unknown"

		try:
			vlan_output = ""
			while len(vlan_output) < 10:
				self._sendCommand("show vlan")
				vlan_output = self.connection.read_until("#", GLOBAL_TIMEOUT)
				if self.L2Debug:
					self.logger.debug(vlan_output)

				if len(vlan_output) < 10:
					self._reconnect()

		except Exception as e:
			if not self._silence_errors:
				self.logger.error("Error while refreshing VLAN status, silenced until reconnection attempts work.")
				self.logger.debug("error: " + str(e))
				self._silence_errors = True
			return False

		self._silence_errors = False
		for vlan in vlan_output.splitlines():

			try:
				vlan_no = int(vlan.split()[0])
			except:
				continue

			if len(vlan.split()) == 5:
				untaggedcolumn = 3
			else:
				untaggedcolumn = 2

			if "TRANSMITTER" in vlan:
				TxDev = None
				for Tx in self.Tx:
					if Tx.vlan == vlan_no:
						TxDev = Tx							
						break

				self.logger.debug("Evaluating VLAN " + str(vlan_no))

				for port in vlan.split()[untaggedcolumn].split(","):
					if "-" in port:
						rx_ports_start = int(port[2:port.index("-")])
						rx_ports_end = int(port[port.index("-") + 1:])

						self.logger.debug("Found that Tx " + str(vlan_no - 10) + " is being watched by the Rx's in port " + str(rx_ports_start) + " through " + str(rx_ports_end))

						i = rx_ports_start
						while i <= rx_ports_end:
							for Rx in self.Rx:
								if Rx.port == i:
									self.logger.debug("Marked that Rx " + str(Rx.no) + " is watching VLAN " + str(vlan_no))
									Rx.vlan_watching = vlan_no
									i = i + 1

									self.logger.debug("Marked that Tx " + str(TxDev.no) + " is being watched by Rx " + str(Rx.no))

									if TxDev.being_watched == "Not in use":
										TxDev.being_watched = str(Rx.no)
									else:
										TxDev.being_watched = TxDev.being_watched + ", " + str(Rx.no)								

									break
									
					else:
						try:
							RxPortNo = int(port[2:])
						except:
							continue

						self.logger.debug("Evaluating port " + str(RxPortNo) + " for VLAN " + str(vlan_no))

						for Rx in self.Rx:
							if Rx.port == RxPortNo:
								Rx.vlan_watching = vlan_no
								self.logger.debug("Found that Tx " + str(TxDev.no) + " is being watched by Rx " + str(Rx.no))

								self.logger.debug("Marked that Rx " + str(Rx.no) + " is watching VLAN " + str(vlan_no))

								if TxDev.being_watched == "Not in use":
									TxDev.being_watched = str(Rx.no)
								else:
									TxDev.being_watched = TxDev.being_watched + ", " + str(Rx.no)								

								break

		for dev in self.Tx:
			self.logger.debug("Transmitter " + str(dev.no) + " being watched by: " + str(dev.being_watched))

		for dev in self.Rx:
			self.logger.debug("Rx " + str(dev.no) + " watching: " + str(dev.vlan_watching))

		self.logger.debug("finished device update")

	def _loadConfiguration(self):
		self.logger.debug("starting VLAN processing")
		self._sendCommand("show vlan")
		vlan_output = self.connection.read_until("#", GLOBAL_TIMEOUT)
		if self.L2Debug:
			self.logger.debug(vlan_output)

		self._sendCommand("show ip int")
		ip_int = self.connection.read_until("#", GLOBAL_TIMEOUT)
		self.logger.debug(ip_int)

		for vlan in vlan_output.splitlines():

			try:
				vlan_no = int(vlan.split()[0])
			except:
				continue

			if len(vlan.split()) == 5:
				untaggedcolumn = 3
			else:
				untaggedcolumn = 2

			# Look for VLAN 10 which is the VLAN for JAP Recievers, record some information because I have to process the Tx's first before I can process the Rx's
			if vlan_no == 10:
				self.receiverVLAN = 10

				rx_ports = vlan.split()[untaggedcolumn]
				rx_ports_start = int(rx_ports[2:rx_ports.index("-")])
				rx_ports_end = int(rx_ports[rx_ports.index("-") + 1:])

			elif "TRANSMITTER" in vlan:
				tx_ip = "UNKNOWN"

				for ip in ip_int.splitlines():
					if len(ip.split()) < 3:
						continue

					if ip.split()[1] + " " + ip.split()[2] == "vlan " + str(vlan_no):
						tx_ip = ip.split()[0][:-3]
						last_num = int(tx_ip[tx_ip.rindex(".") + 1:]) + 1
						tx_ip = tx_ip[:tx_ip.rindex(".")] + "." + str(last_num)
						break

				self.logger.debug("Adding Transmitter no. " + str(vlan_no - 10) + ", VLAN: " + str(vlan_no) + ", IP: " + tx_ip )
				newTx = JustAddPowerTransmitter(vlan_no, tx_ip, self.firmware, self.logger, self.L2Debug)
				self._addTransmitter(newTx)


		### END processing VLAN Output

		## Now that we have processed the Tx's, we can finish with the Rx's
		rx_ports_start = rx_ports_start + self.TxCount
		self.logger.debug("Detected that receivers are on ports " + str(rx_ports_start) + " to " + str(rx_ports_end))

		rx_port = rx_ports_start
		rx_ip = "UNKNOWN"
		vlan_no = self.receiverVLAN
		
		while self.RxCount <= rx_ports_end - rx_ports_start:
			for ip in ip_int.splitlines():
				if len(ip.split()) < 3:
					continue

				if ip.split()[1] + " " + ip.split()[2] == "vlan " + str(vlan_no):
					rx_ip = ip.split()[0][:-3]
					last_num = int(rx_ip[rx_ip.rindex(".") + 1:]) + self.RxCount + 1
					rx_ip = rx_ip[:rx_ip.rindex(".")] + "." + str(last_num)
					break

			self.logger.debug("Adding Reciever no. " + str(self.RxCount + 1) + ", VLAN: " + str(vlan_no) + ", IP: " + rx_ip + ", Port: " + str(rx_port))
			newRx = JustAddPowerReceiver(vlan_no, self.RxCount + 1, rx_ip, rx_port, self.firmware, self.logger, self.L2Debug)
			self._addReceiver(newRx)
			rx_port = rx_port + 1


		self.logger.debug("end of VLAN processing")

	def reboot(self):
		command = "reload"

		self._sendCommand(command)
		time.sleep(1)
		self._sendCommand("Y")
		time.sleep(1)
		self._sendCommand("Y")

		answer = self.connection.read_eager()

		self.logger.debug(answer)

	def is_Connected(self):
		if self.connection is None:
			return False

		try:
			self.connection.write("\r\n")

			output_test = self.connection.read_until("#", GLOBAL_TIMEOUT)

			if self.L2Debug:
				self.logger.debug(output_test)

			return True
		except:
			return False

		return False

	def _sendCommand(self, cmd):
		try:
			self.logger.debug(u"Sending network command:  %s" % cmd)
			cmd = cmd + "\r\n"
			self.connection.write(str(cmd))
		except Exception as e:
			self.logger.error("problem sending command, retrying...")
			self._reconnect()
			self.logger.debug(u"Sending network command:  %s" % cmd)
			cmd = cmd + "\r\n"
			self.connection.write(str(cmd))


