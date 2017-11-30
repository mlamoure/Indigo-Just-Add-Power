import socket
import telnetlib
import time

class JAPDevice(object):
	def __init__(self, vlan, ip, logger):
		self.vlan = vlan
		self.ip = ip
		self.logger = logger
		self.connection = None

	def _sendCommand(self, cmd):
		if not self.is_Connected():
			self.logger.debug("was not connected, connecting...")
			self._connect()

		try:
			self.logger.debug(u"Sending command:  %s" % cmd)
			cmdsend = cmd + "\r\n"
			self.connection.write(str(cmdsend))
			self.logger.debug(self.connection.read_until("#"))
			self.logger.info(cmd + " command was sucessfull")
		except:
			self.logger.error("problem sending command")

	def is_Connected(self):
		if self.connection is None:
			return False

		try:
			self.connection.write("echo test\r\n\n")
			return True
		except:
			return False

	def enableImagePull(self, res=320, prior=1, rate=3):
		self.logger.debug("enabling image pull")

		command = "astparam s pull_on_boot " + str(res) + "_" + str(prior) + "_" + str(rate) + ";astparam save;reboot "

		self.logger.debug("sending command: " + command)
		self._sendCommand(command)

	def disableImagePull(self):
		self.logger.debug("disabling image pull")

		command = "astparam s pull_on_boot n;astparam save;reboot "

		self.logger.debug("sending command:" + command)
		self._sendCommand(command)

	def reboot(self):
		command = "reboot"

		self._sendCommand(command)

	def _connect(self):
		isConnectedRetry = 0
		while not self.is_Connected():
			isConnectedRetry += 1

			if isConnectedRetry > 10:
				self.logger.debug("Maximum number of connection retries has occured.")
				return

			try:
				self.logger.info(u"Connecting to JAP Device via IP  %s" % self.ip)
				self.connection = telnetlib.Telnet(self.ip, 23)
				time.sleep(3)

			except:
				self.logger.exception("Connection attempt failed. %s" % e.message)
				time.sleep(5)

		self.logger.debug("Connected")

class JustAddPowerTransmitter(JAPDevice):
	def __init__(self, vlan, ip, logger):
		super(JustAddPowerTransmitter, self).__init__(vlan, ip, logger)

#		self.vlan = int(vlan)
#		self.ip = ip

		self.no = int(vlan) - 10
		self.ignore = False  # Set this to true for a device that is set up in JADConfig but isn't actually being used. e.g. when you make a larger matrix to give yourself headroom.
		self.being_watched = "Unknown"

class JustAddPowerReceiver(JAPDevice):
	def __init__(self, vlan, no, ip, port, logger):
		super(JustAddPowerReceiver, self).__init__(vlan, ip, logger)
#		self.vlan = int(vlan)
#		self.ip = ip

		self.no = int(no)
		self.ignore = False # Set this to true for a device that is set up in JADConfig but isn't actually being used. e.g. when you make a larger matrix to give yourself headroom.
		self.port = port
		self.vlan_watching = "Unknown"

class JustAddPowerMatrix(object):
	"""docstring for JustAddPowerMatrix"""
	def __init__(self, model, ip, login, password, controlVLAN = 2, logger = None):
		super(JustAddPowerMatrix, self).__init__()
		self.model = model
		self.ip = ip
		self.login = login
		self.password = password
		self.RxCount = 0
		self.TxCount = 0
		self.controlVLAN = controlVLAN
		self.receiverVLAN = 10

		self.Rx = []
		self.Tx = []

		self.logger = logger

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

		output = self.connection.read_until("#")
		self.logger.debug(output)


	def _addReceiver(self, newRx):
		self.Rx.append(newRx)
		self.RxCount = self.RxCount + 1

	def _addTransmitter(self, newTx):	
		self.Tx.append(newTx)
		self.TxCount = self.TxCount + 1

	def isConfigured(self):
		return len(self.ip) > 0 and len(self.password) > 0 and len(self.login) > 0

	def _connect(self):

		if not self.isConfigured():
			return

		self.timeout = 1

		try:
			self.logger.info(u"Connecting to switch via IP to %s" % self.ip)
			self.connection = telnetlib.Telnet(self.ip, 23)
			time.sleep(3)


			a = self.connection.read_until("User Name:", self.timeout)
			self.logger.debug(u"Telnet: %s" % a)

			if 'User' in a:
				self.logger.debug(u"Sending username.")
				self.connection.write(str(self.login) + "\r\n")

				a = self.connection.read_until("Password:", self.timeout)
				self.logger.debug(u"Telnet: %s" % a)

				if 'Password' in a:
					self.logger.debug(u"Sending password.")
					self.connection.write(str(self.password) + "\r\n")

					a = self.connection.read_until("#")
					self.logger.debug(u"Telnet: %s" % a)

				else:
					self.logger.debug(u"password failure.")
			else:
				self.logger.debug(u"username failure.")

			self.logger.info(u"Connected to switch")
			self.logger.debug(u"End of connection process.")

		except socket.error, e:
			self.logger.exception(u"Unable to connect. %s" % e.message)

	def updatePorts(self):
		self.logger.debug("starting port refresh")

		for dev in self.Tx:
			dev.being_watched = "Not in use"

		for dev in self.Rx:
			dev.vlan_watching = "Unknown"

		try:
			self._sendCommand("show vlan")
			vlan_output = self.connection.read_until("#")
			self.logger.debug(vlan_output)
		except:
			self.logger.error("Error while refreshing VLAN status.")
			return

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


# DEPRECIATED THIS BECAUSE IT IS LESS EFFICIENT THAN PULLING FROM THE SHOW VLAN COMMAND
	def _unused_updatePorts(self):
		self.logger.debug("starting port refresh")

		for dev in self.Tx:
			dev.being_watched = "Not in use"

		for dev in self.Rx:
			self.connection.read_eager()
			self._sendCommand("show int sw gi" + str(dev.port) + "\r\n")
			port_output = self.connection.read_until("Port is member in:")
			self.logger.debug(port_output)
			port_output = self.connection.read_until("Forbidden")
			self.logger.debug(port_output)

			for vlan in port_output.splitlines():
				vlan_no = 10

				try:
					vlan_no = vlan.split()[0]
					vlan_no = int(vlan_no)
				except:
					continue

				if vlan_no < 11:
					continue
				else:
					self.logger.debug("Found that Rx " + str(dev.no) + " is watching VLAN " + str(vlan_no))
					dev.vlan_watching = vlan_no

					for TxDev in self.Tx:
						if TxDev.vlan == vlan_no:
							self.logger.debug("Found that Tx " + str(TxDev.no) + " is being watched by Rx " + str(dev.no))

							if TxDev.being_watched == "Not in use":
								TxDev.being_watched = str(dev.no)
							else:
								TxDev.being_watched = TxDev.being_watched + ", " + str(dev.no)								
							
							break

		self.logger.debug("finished device update")


	def _loadConfiguration(self):
		self.logger.debug("starting VLAN processing")
		self._sendCommand("show vlan")
		vlan_output = self.connection.read_until("#")
		self.logger.debug(vlan_output)

		self._sendCommand("show ip int")
		ip_int = self.connection.read_until("#")
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
				newTx = JustAddPowerTransmitter(vlan_no, tx_ip, self.logger)
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
			newRx = JustAddPowerReceiver(vlan_no, self.RxCount + 1, rx_ip, rx_port, self.logger)
			self._addReceiver(newRx)
			rx_port = rx_port + 1


		self.logger.debug("end of VLAN processing")

	def reboot(self):
		command = "reload"

		self._sendCommand(command)
		self.logger.debug(self.connection.read_until("#"))

	def is_Connected(self):
		if self.connection is None:
			return False

		try:
			self.connection.write("\r\n")
			self.logger.debug(self.connection.read_until("#"))
			return True
		except:
			return False

	def _sendCommand(self, cmd):
		try:
			self.logger.debug(u"Sending network command:  %s" % cmd)
			cmd = cmd + "\r\n"
			self.connection.write(str(cmd))
		except IOError:
			self.logger.error("problem sending command, retrying...")
			self._connect()
			self.logger.debug(u"Sending network command:  %s" % cmd)
			cmd = cmd + "\r\n"
			self.connection.write(str(cmd))


