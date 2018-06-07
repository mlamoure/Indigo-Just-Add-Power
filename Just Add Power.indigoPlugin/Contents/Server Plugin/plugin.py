#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################

import indigo

import os
import sys
import datetime
import time
import requests
import json
import shutil
from PIL import Image
from distutils.version import LooseVersion

from JAP import JustAddPowerMatrix
from JAP import JustAddPowerTransmitter
from JAP import JustAddPowerReceiver

DEFAULT_UPDATE_FREQUENCY = 24 # frequency of update check

################################################################################
class Plugin(indigo.PluginBase):
	########################################
	def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
		super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
		self.matrixList = []
		self.debug = pluginPrefs.get("debug", False)
		self.L2Debug = pluginPrefs.get("L2Debug", False)
		self.image_pull = pluginPrefs.get("image_pull", False)
		self.image_pull_dir = pluginPrefs.get("image_pull_dir", "")
		self.image_pull_refresh = pluginPrefs.get("image_pull_refresh", 10)

		if self.image_pull:
			self.pollingInterval = self.image_pull_refresh
		else:
			self.pollingInterval = 45

		self.lastUpdateCheck = None
		self.indigoVariablesFolderName = "JAP Image Pull"
		self.indigoVariablesFolderID = None	

	########################################
	def startup(self):
		self.debugLog(u"startup called")
		self.updateDeviceFolder()
		self.version_check()

	def checkForUpdates(self):
		self.version_check()

	def closedPrefsConfigUi(self, valuesDict, userCancelled):
		if not userCancelled:
			self.debug = valuesDict["debug"]
			self.L2Debug = valuesDict["L2Debug"]

			self.image_pull = valuesDict["image_pull"]
			self.image_pull_dir = valuesDict["image_pull_dir"]
			self.image_pull_refresh = valuesDict["image_pull_refresh"]

			if self.image_pull:
				self.pollingInterval = self.image_pull_refresh
			else:
				self.pollingInterval = 45

	def version_check(self):
		pluginId = self.pluginId
		self.lastUpdateCheck = datetime.datetime.now()		

		# Create some URLs we'll use later on
		current_version_url = "https://api.indigodomo.com/api/v2/pluginstore/plugin-version-info.json?pluginId={}".format(pluginId)
		store_detail_url = "https://www.indigodomo.com/pluginstore/{}/"
		try:
			# GET the url from the servers with a short timeout (avoids hanging the plugin)
			reply = requests.get(current_version_url, timeout=5)
			# This will raise an exception if the server returned an error
			reply.raise_for_status()
			# We now have a good reply so we get the json
			reply_dict = reply.json()
			plugin_dict = reply_dict["plugins"][0]
			# Make sure that the 'latestRelease' element is a dict (could be a string for built-in plugins).
			latest_release = plugin_dict["latestRelease"]
			if isinstance(latest_release, dict):
				# Compare the current version with the one returned in the reply dict
				if LooseVersion(latest_release["number"]) > LooseVersion(self.pluginVersion):
				# The release in the store is newer than the current version.
				# We'll do a couple of things: first, we'll just log it
				  self.logger.info(
					"A new version of the plugin (v{}) is available at: {}".format(
						latest_release["number"],
						store_detail_url.format(plugin_dict["id"])
					)
				)
		except Exception as exc:
			self.logger.error(unicode(exc))

	def shutdown(self):
		self.debugLog(u"shutdown called")

	def runConcurrentThread(self):
		self.logger.debug("Starting concurrent tread")

		self.sleep(1)
		
		try:
			while True:
				self.updateAllStates()
				self.updateVariables()
				self.performImagePull()

				self.sleep(int(self.pollingInterval))

				if self.lastUpdateCheck < datetime.datetime.now()-datetime.timedelta(hours=DEFAULT_UPDATE_FREQUENCY):
					self.version_check()

		except self.StopThread:
			self.logger.debug("Received StopThread")

	def deviceStopComm(self, dev):
		self.debugLog(u"deviceStopComm: %s" % (dev.name,))

	def getDeviceDisplayStateId(self, dev):
		if dev.deviceTypeId == "matrix":
			stateId = dev.pluginProps.get(u'stateDisplayColumnState', u'connectionState_ui')
		elif dev.deviceTypeId == "transmitter":
			stateId = dev.pluginProps.get(u'stateDisplayColumnState', u'being_watched_ui')
		elif dev.deviceTypeId == "receiver":
			stateId = dev.pluginProps.get(u'stateDisplayColumnState', u'vlan_watching_ui')

		self.logger.debug("Returning state for device " + dev.name + ", State column: " + stateId)
		return stateId

	########################################
	# deviceStartComm() is called on application launch for all of our plugin defined
	# devices, and it is called when a new device is created immediately after its
	# UI settings dialog has been validated. This is a good place to force any properties
	# we need the device to have, and to cleanup old properties.
	def deviceStartComm(self, dev):
		self.debugLog(u"deviceStartComm: %s" % (dev.name,))

		if dev.deviceTypeId == "matrix":
			self.matrixList.append(JustAddPowerMatrix(dev.pluginProps["Model"], dev.pluginProps["ip"], dev.pluginProps["Login"], dev.pluginProps["Password"], dev.pluginProps["ControlVLAN"], self.logger, self.L2Debug))
			self.updateDevices()

			if len(dev.address) < 2:
				props = dev.pluginProps
				props["address"] = dev.pluginProps["ip"]
				dev.replacePluginPropsOnServer(props)

		self.updateDeviceStates(dev)


	def performImagePull(self):
		if not self.image_pull:
			return

		for matrix in self.matrixList:
			for Rx in matrix.Rx:
				dev = self.getIndigoDevice(Rx)

				Rx.friendlyname = dev.name

				if dev is None:
					pass

				# Doing a little cleanup here.  It seems we wernt always saving this correctly.
				if dev.pluginProps["ignore"]:
					Rx.ignore = True

				if Rx.ignore or not Rx.ImagePullEnabled():
					self.logger.debug("not getting image from " + dev.name + " as device is ignored or image pull is disabled.")
					pass

				if not Rx.ignore:
					result = self.getImage(Rx.image_pull_url, self.image_pull_dir + "/Rx" + str(Rx.no) + ".bmp")

					if result:
						self.convertImage(self.image_pull_dir + "/Rx" + str(Rx.no) + ".bmp")

			for Tx in matrix.Tx:
				dev = self.getIndigoDevice(Tx)

				Tx.friendlyname = dev.name

				if dev is None:
					pass

				if Rx.ignore or not Tx.ImagePullEnabled():
					self.logger.debug("not getting image from " + dev.name + " as device is ignored or image pull is disabled.")
					pass

				# Doing a little cleanup here.  It seems we wernt always saving this correctly.
				if dev.pluginProps["ignore"]:
					Tx.ignore = True

				if not Tx.ignore:
					result = self.getImage(Tx.image_pull_url, self.image_pull_dir + "/Tx" + str(Tx.no) + ".bmp")

					if result:
						self.convertImage(self.image_pull_dir + "/Tx" + str(Tx.no) + ".bmp")


	def getImage(self, url, save):
		self.logger.debug("getting image: " + url + " and saving it to: " + save)

		try:
			r = requests.get(url, stream=True, timeout=5)

			if r.status_code == 200:
				with open(save, 'wb') as f:
					r.raw.decode_content = True
					shutil.copyfileobj(r.raw, f)
			else:			
				self.logger.debug("   error getting image pull.  Status code: " + str(r.status_code))
				del r
				return False

			del r
			self.logger.debug("   completed")
			return True
		except requests.exceptions.Timeout:
			self.logger.debug("   the request timed out.")
		except Exception as e:
			self.logger.debug("   error getting image. error: " + str(e))
			return False


	def convertImage(self, image):
		self.logger.debug("converting image: " + image)
		try:	
			if image is not None:
				img = Image.open(image)
				img.save(image[:-3] + "jpg", 'jpeg')
				return True
		except Exception as e:
			self.logger.error("Error converting image: " + str(e))
			return False

	def updateAllStates(self, dev = None):
		self.logger.debug("Started update all states")
		for matrix in self.matrixList:

			if dev == None:
				matrix.updatePorts()
			elif dev.pluginProps["ip"] == matrix.ip:
				matrix.updatePorts()

		for dev in [s for s in indigo.devices.iter(filter="self") if s.enabled]:
			self.updateDeviceStates(dev)
		self.logger.debug("Finished update all states")

	def updateDeviceStates(self, dev):
		self.logger.debug("Started state update for " + dev.name)
		if dev.deviceTypeId == "matrix":
			selMatrix = None
			for matrix in self.matrixList:
				if dev.pluginProps["ip"] == matrix.ip:
					selMatrix = matrix
					break

			dev.updateStateOnServer(key="connectionState", value=str(selMatrix.is_Connected()))

			if selMatrix.is_Connected():
				dev.updateStateOnServer(key="connectionState_ui", value="Connected")					
			else:
				dev.updateStateOnServer(key="connectionState_ui", value="Not Connected")					

		elif dev.deviceTypeId == "transmitter" and "matrix" in dev.pluginProps:
			matrixDev = indigo.devices[dev.pluginProps["matrix"]]

			selMatrix = self.getJAPDevice(matrixDev)

			if selMatrix is None:
				return

			Tx = self.getJAPDevice(dev)
			Tx.friendlyname = dev.name

			if "ignore" in dev.pluginProps:
				Tx.ignore = dev.pluginProps["ignore"]

			being_watched_ui = str(Tx.being_watched)
			if dev.states["being_watched"] != str(Tx.being_watched):

				if Tx.being_watched != "Not in use" and Tx.being_watched != "Unknown":
					being_watched_ui = ""

					for Rx in Tx.being_watched.strip().split(","):
#								self.logger.debug(dev.name + " update: looking for Rx. " + str(Rx.strip()))
						for RxDev in [s for s in indigo.devices.iter(filter="self.receiver") if s.enabled]:
							if RxDev.pluginProps["no"] == int(Rx.strip()) and not RxDev.pluginProps["ignore"]:

								if len(being_watched_ui) == 0:
									being_watched_ui = "Rx. " + str(Rx) + " (" + RxDev.name + ")"
								else:
									being_watched_ui = being_watched_ui + ", Rx. " + str(Rx) + " (" + RxDev.name + ")"
								break

				# This will happen if all the Rx's are ignored that this Tx is watching
				if len(being_watched_ui) == 0:
					being_watched_ui = "Not in use"
				
				if not "ignore" in dev.pluginProps:
					props = dev.pluginProps
					props["ignore"] = False
					dev.replacePluginPropsOnServer(props)

				if being_watched_ui != "Unknown" and not dev.pluginProps["ignore"]:
					indigo.server.log(dev.name + " updated to now sending to " + being_watched_ui)
				
				dev.updateStateOnServer(key="being_watched", value=Tx.being_watched)
				dev.updateStateOnServer(key="being_watched_ui", value=being_watched_ui)
				dev.updateStateOnServer(key="image_pull_enabled", value=Tx.ImagePullEnabled())

		elif dev.deviceTypeId == "receiver" and "matrix" in dev.pluginProps:
			matrixDev = indigo.devices[dev.pluginProps["matrix"]]

			selMatrix = self.getJAPDevice(matrixDev)

			if selMatrix is None:
				return

			Rx = self.getJAPDevice(dev)
			Rx.friendlyname = dev.name

			if dev.pluginProps["ignore"]:
				vlan_watching_ui = "Not in use"
				vlan_watching = ""
				dev.updateStateOnServer(key="vlan_watching", value=Rx.vlan_watching)
				dev.updateStateOnServer(key="vlan_watching_ui", value=vlan_watching_ui)

			elif str(dev.states["vlan_watching"]) != str(Rx.vlan_watching):
				vlan_watching_ui = "unknown"
				if Rx.vlan_watching != "Unknown":
					for TxDev in [s for s in indigo.devices.iter(filter="self.transmitter") if s.enabled]:
						if TxDev.pluginProps["vlan"] == Rx.vlan_watching:
							vlan_watching_ui = "VLAN " + str(Rx.vlan_watching) + " (" + TxDev.name + ")"
							break

					if not dev.pluginProps["ignore"]:
						indigo.server.log(dev.name + " updated to now watching " + vlan_watching_ui)

				dev.updateStateOnServer(key="vlan_watching", value=Rx.vlan_watching)
				dev.updateStateOnServer(key="vlan_watching_ui", value=vlan_watching_ui)
				dev.updateStateOnServer(key="image_pull_enabled", value=Rx.ImagePullEnabled())

		self.logger.debug("Completed state update for " + dev.name)


	def updateDeviceFolder(self):
		self.indigoDevicesFolderName = "JAP Devices"
		try:
			indigo.devices.folder.create(self.indigoDevicesFolderName)
			self.myLog("all",self.indigoDevicesFolderName+ u" folder created")
		except:
			pass
		self.indigoDeviceFolderID = indigo.devices.folders[self.indigoDevicesFolderName].id

	def refresh(self, pluginAction, dev):
		self.updateAllStates(dev)

	def rebootJAP(self, pluginAction, dev):
		selMatrix = None
		for matrix in self.matrixList:
			if dev.pluginProps["ip"] == matrix.ip:
				selMatrix = matrix

		device = None
		for JAPDevice in selMatrix.allDevices():
			if indigo.devices[int(pluginAction.props["device"])].pluginProps["ip"] == JAPDevice.ip:
				device = JAPDevice

		if device.reboot():
			indigo.server.log("rebooted " + device.friendlyname)
		
	def rebootSwitch(self, pluginAction, dev):
		for matrix in self.matrixList:
			if dev.pluginProps["ip"] == matrix.ip:
				indigo.server.log("rebooting " + dev.name)
				matrix.reboot()

	def imagepull(self, pluginAction, dev):
		selMatrix = self.getJAPDevice(dev)

		if selMatrix is None:
			self.logger.error("error while executing the action")
			return

		device = self.getJAPDevice(indigo.devices[int(pluginAction.props["device"])])

		if device is None:
			self.logger.error("error while executing the action")
			return

		if pluginAction.props["enableDisable"] == "enable":
			if device.enableImagePull(pluginAction.props["resolution"], pluginAction.props["priority"], pluginAction.props["rate"]):
					indigo.server.log("enabled imagepull for " + device.friendlyname)				
		else:
			if device.ImagePullEnabled():
				if device.disableImagePull():
					indigo.server.log("disabled imagepull for " + device.friendlyname)
			else:
				self.logger.error("image pull is already disabled for " + device.friendlyname + ", ignoring")


	def switch(self, pluginAction, dev):
		for matrix in self.matrixList:
			if dev.pluginProps["ip"] == matrix.ip:

				switchRx = self.getJAPDevice(indigo.devices[int(pluginAction.props["Rx"])].pluginProps["ip"])
				switchTx = self.getJAPDevice(indigo.devices[int(pluginAction.props["Tx"])].pluginProps["ip"])

				matrix.watch(switchRx, switchTx)
				break

	def getRxSelector(self, filter=u'', valuesDict=None, typeId=u'', targetId=0):
		matrixdev = indigo.devices[targetId]
		selMatrix = None
		availableRx = []

		selMatrix = self.getJAPDevice(matrixdev)

		if selMatrix is None:
			self.logger.error("error while executing the selector")
			return

		for RxDev in [s for s in indigo.devices.iter(filter="self.receiver") if s.enabled]:
			for Rx in selMatrix.Rx:
				if RxDev.pluginProps["ip"] == Rx.ip:
					value = RxDev.id
					text = RxDev.name

					availableRx.append((value, text))

		return availableRx

	def availableMatrix(self, filter=u'', valuesDict=None, typeId=u'', targetId=0):
		availableMatrix = []
		for matrix in [s for s in indigo.devices.iter(filter="self.matrix") if s.enabled]:
			value = matrix.id
			text = matrix.name

			availableMatrix.append((value, text))

		return availableMatrix


	def getRxTxSelector(self, filter=u'', valuesDict=None, typeId=u'', targetId=0):
		return self.getRxSelector(filter, valuesDict, typeId, targetId) + self.getTxSelector(filter, valuesDict, typeId, targetId)

	def getTxSelector(self, filter=u'', valuesDict=None, typeId=u'', targetId=0):
		matrixdev = indigo.devices[targetId]
		selMatrix = None
		availableTx = []

		selMatrix = self.getJAPDevice(matrixdev)

		if selMatrix is None:
			self.logger.error("error while executing the selector")
			return

		for TxDev in [s for s in indigo.devices.iter(filter="self.transmitter") if s.enabled]:
			for Tx in selMatrix.Tx:
				if TxDev.pluginProps["ip"] == Tx.ip:
					value = TxDev.id
					text = TxDev.name

					availableTx.append((value, text))

		return availableTx

	def updateDevices(self):
		for matrix in self.matrixList:

			matrix_dev = self.getIndigoDevice(matrix)

			if matrix_dev is None:
				return

			for Rx in matrix.Rx:
				devExists = False
				devName = ""

				if len(self.matrixList) > 1:
					devName = matrix_dev.name + " Rx. " + str(Rx.no)
				else:
					devName = "JAP Rx. " + str(Rx.no)

				devAddr = Rx.ip
				devDesc = devName

				for dev in indigo.devices.iter("com.vtmikel.justaddpower"):
					if dev.address == Rx.ip:
						devExists = True
						break

				if not devExists:
					indigo.server.log("automatically creating " + devName)
					indigo.device.create(
						protocol=indigo.kProtocol.Plugin,
						address=devAddr,
						name=devName,
						description=devDesc,
						pluginId="com.vtmikel.justaddpower",
						deviceTypeId="receiver",
						folder=self.indigoDeviceFolderID
					)
				
					dev = indigo.devices[devName]
					props = dev.pluginProps

					props["matrix"] = matrix_dev.id
					props["no"] = Rx.no
					props["ip"] = Rx.ip
					props["ignore"] = False

					dev.replacePluginPropsOnServer(props)


			for Tx in matrix.Tx:
				devExists = False
				devName = ""

				if len(self.matrixList) > 1:
					devName = matrix_dev.name + " Tx. " + str(Tx.no)
				else:
					devName = "JAP Tx. " + str(Tx.no)

				devAddr = Tx.ip
				devDesc = devName

				for dev in indigo.devices.iter("com.vtmikel.justaddpower"):
					if dev.address == Tx.ip:
						devExists = True
						break

				if not devExists:
					indigo.server.log("automatically creating " + devName + " (VLAN: " + str(Tx.vlan) + ")")
					indigo.device.create(
						protocol=indigo.kProtocol.Plugin,
						address=devAddr,
						name=devName,
						description=devDesc,
						pluginId="com.vtmikel.justaddpower",
						deviceTypeId="transmitter",
						folder=self.indigoDeviceFolderID
					)
				
					dev = indigo.devices[devName]
					props = dev.pluginProps

					props["matrix"] = matrix_dev.id
					props["ip"] = Tx.ip
					props["vlan"] = Tx.vlan
					props["ignore"] = False

					dev.replacePluginPropsOnServer(props)


	########################################
	def validateDeviceConfigUi(self, valuesDict, typeId, devId):
		return (True, valuesDict)

	def createVariableFolder(self, variableFolderName):
		if variableFolderName is None:
			return

		# CREATE THE Varaible Folder
#		if variableFolderName != self.indigoVariablesFolderName:
		self.indigoVariablesFolderName = variableFolderName

		if self.indigoVariablesFolderName not in indigo.variables.folders:
			self.indigoVariablesFolderID = indigo.variables.folder.create(self.indigoVariablesFolderName).id
			indigo.server.log(self.indigoVariablesFolderName+ u" folder created")
		else:
			self.indigoVariablesFolderID=indigo.variables.folders[self.indigoVariablesFolderName].id

	def getIndigoDevice(self, JAPDevice):
		if type(JAPDevice) is JustAddPowerTransmitter:
			for dev in [s for s in indigo.devices.iter(filter="self.transmitter") if s.enabled]:
				if dev.pluginProps["ip"] == JAPDevice.ip:
					return dev


		elif type(JAPDevice) is JustAddPowerReceiver:
			for dev in [s for s in indigo.devices.iter(filter="self.receiver") if s.enabled]:
				if dev.pluginProps["ip"] == JAPDevice.ip:
					return dev

		return None

	def getJAPDevice(self, indigoDevice):

		if indigoDevice.deviceTypeId == "matrix":
			for matrix in self.matrixList:
				if indigoDevice.pluginProps["ip"] == matrix.ip:
					return matrix

		matrixDev = indigo.devices[indigoDevice.pluginProps["matrix"]]
		selMatrix = None
		for matrix in self.matrixList:
			if matrixDev.pluginProps["ip"] == matrix.ip:
				selMatrix = matrix
				break

		if selMatrix is None:
			self.logger.debug("no matrix was found for device: " + indigoDevice.name)
			return None

		if indigoDevice.deviceTypeId == "receiver":
			for Rx in selMatrix.Rx:
				if indigoDevice.pluginProps["ip"] == Rx.ip:
					return Rx

		if indigoDevice.deviceTypeId == "transmitter":
			for Tx in selMatrix.Tx:
				if indigoDevice.pluginProps["ip"] == Tx.ip:
					return Tx

		return None

	def updateVariables(self):
		self.logger.debug("started variable updates")
		if self.indigoVariablesFolderID is not None:
			for dev in [s for s in indigo.devices.iter(filter="self") if s.enabled]:

				if dev.deviceTypeId != "matrix":
					RxTxDevice = self.getJAPDevice(dev)

					if RxTxDevice is None:
						continue

					# Image pull URLS
					varName = dev.name.replace(' ', '_').replace(".", "").replace("-", "_") + "_image_pull_url"
					if not varName in indigo.variables:
						self.logger.debug("Created variables for device " + dev.name)
						indigo.variable.create(varName,folder=self.indigoVariablesFolderID)

					varValue = RxTxDevice.image_pull_url

					if indigo.variables[varName].value != varValue:
						self.logger.debug("Updated variable value for device " + dev.name)
						indigo.variable.updateValue(varName, varValue)

					if self.image_pull:
						# Image pull converted files
						varName = dev.name.replace(' ', '_').replace(".", "").replace("-", "_") + "_image_pull_file_url"
						if not varName in indigo.variables:
							self.logger.debug("Created variables for image pull for device " + dev.name)
							indigo.variable.create(varName,folder=self.indigoVariablesFolderID)

						if dev.deviceTypeId == "transmitter":
							varValue = "file://" + self.image_pull_dir + "/Tx" + str(RxTxDevice.no) + ".jpg"
						else:
							varValue = "file://" + self.image_pull_dir + "/Rx" + str(RxTxDevice.no) + ".jpg"

						if indigo.variables[varName].value != varValue:
							self.logger.debug("Updated variable value for device " + dev.name)
							indigo.variable.updateValue(varName, varValue)

		else:
			self.createVariableFolder(self.indigoVariablesFolderName)

		self.logger.debug("finished variable updates")

