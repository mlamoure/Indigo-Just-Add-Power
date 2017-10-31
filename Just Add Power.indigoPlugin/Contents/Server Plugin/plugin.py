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
from ghpu import GitHubPluginUpdater
from JAP import JustAddPowerMatrix


DEFAULT_UPDATE_FREQUENCY = 24 # frequency of update check

################################################################################
class Plugin(indigo.PluginBase):
	########################################
	def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
		super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
		self.pollingInterval = 45
		self.matrixList = []
		self.debug = pluginPrefs.get("debug", False)

		self.updater = GitHubPluginUpdater(self)
		self.updater.checkForUpdate(str(self.pluginVersion))
		self.lastUpdateCheck = datetime.datetime.now()
		self.indigoVariablesFolderName = "JAP Image Pull"
		self.indigoVariablesFolderID = None	

	########################################
	def startup(self):
		self.debugLog(u"startup called")
		self.updateDeviceFolder()

	def checkForUpdates(self):
		self.updater.checkForUpdate()

	def closedPrefsConfigUi(self, valuesDict, userCancelled):
		if not userCancelled:
			self.debug = valuesDict["debug"]

	def updatePlugin(self):
		self.updater.update()

	def shutdown(self):
		self.debugLog(u"shutdown called")

	def runConcurrentThread(self):
		self.logger.debug("Starting concurrent tread")

		self.sleep(1)
		
		try:
			while True:
				self.updateAllStates()
				self.updateVariables()
				self.sleep(int(self.pollingInterval))

				if self.lastUpdateCheck < datetime.datetime.now()-datetime.timedelta(hours=DEFAULT_UPDATE_FREQUENCY):
					self.updater.checkForUpdate(str(self.pluginVersion))
					self.lastUpdateCheck = datetime.datetime.now()		

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
			self.matrixList.append(JustAddPowerMatrix(dev.pluginProps["Model"], dev.pluginProps["ip"], dev.pluginProps["Login"], dev.pluginProps["Password"], dev.pluginProps["ControlVLAN"], self.logger))
			self.updateDevices()

			if len(dev.address) < 2:
				props = dev.pluginProps
				props["address"] = dev.pluginProps["ip"]
				dev.replacePluginPropsOnServer(props)

		self.updateDeviceStates(dev)


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


			dev.updateStateOnServer(key="connectionState", value=str(selMatrix.connected))

			if selMatrix.connected:
				dev.updateStateOnServer(key="connectionState_ui", value="Connected")					
			else:
				dev.updateStateOnServer(key="connectionState_ui", value="Not Connected")					

		elif dev.deviceTypeId == "transmitter" and "matrix" in dev.pluginProps:
			matrixDev = indigo.devices[dev.pluginProps["matrix"]]

			selMatrix = None
			for matrix in self.matrixList:
				if matrixDev.pluginProps["ip"] == matrix.ip:
					selMatrix = matrix
					break

			for Tx in selMatrix.Tx:
				if Tx.ip == dev.pluginProps["ip"]:
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

							indigo.server.log(dev.name + " updated to now sending to " + being_watched_ui)
						dev.updateStateOnServer(key="being_watched", value=Tx.being_watched)
						dev.updateStateOnServer(key="being_watched_ui", value=being_watched_ui)
					break

		elif dev.deviceTypeId == "receiver" and "matrix" in dev.pluginProps:
			matrixDev = indigo.devices[dev.pluginProps["matrix"]]

			selMatrix = None
			for matrix in self.matrixList:
				if matrixDev.pluginProps["ip"] == matrix.ip:
					selMatrix = matrix
					break

			for Rx in selMatrix.Rx:
				if Rx.ip == dev.pluginProps["ip"]:
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
							indigo.server.log(dev.name + " updated to now watching " + vlan_watching_ui)

						dev.updateStateOnServer(key="vlan_watching", value=Rx.vlan_watching)
						dev.updateStateOnServer(key="vlan_watching_ui", value=vlan_watching_ui)

					break

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

		device.reboot()
		
	def rebootSwitch(self, pluginAction, dev):
		for matrix in self.matrixList:
			if dev.pluginProps["ip"] == matrix.ip:
				matrix.reboot()

	def imagepull(self, pluginAction, dev):
		selMatrix = None
		for matrix in self.matrixList:
			if dev.pluginProps["ip"] == matrix.ip:
				selMatrix = matrix

		device = None
		for JAPDevice in selMatrix.allDevices():
			if indigo.devices[int(pluginAction.props["device"])].pluginProps["ip"] == JAPDevice.ip:
				device = JAPDevice

		if pluginAction.props["enableDisable"] == "enable":
			device.enableImagePull(pluginAction.props["resolution"], pluginAction.props["priority"], pluginAction.props["rate"])
		else:
			device.disableImagePull()


	def switch(self, pluginAction, dev):
		for matrix in self.matrixList:
			if dev.pluginProps["ip"] == matrix.ip:

				for Rx in matrix.Rx:
					devIP = indigo.devices[int(pluginAction.props["Rx"])].pluginProps["ip"]
					if Rx.ip == devIP:
						switchRx = Rx
						break

				for Tx in matrix.Tx:
					devIP = indigo.devices[int(pluginAction.props["Tx"])].pluginProps["ip"]
					if Tx.ip == devIP:
						switchTx = Tx
						break

				matrix.watch(switchRx, switchTx)
				break

	def getRxSelector(self, filter=u'', valuesDict=None, typeId=u'', targetId=0):
		matrixdev = indigo.devices[targetId]
		selMatrix = None
		availableRx = []

		for matrix in self.matrixList:
			if matrixdev.pluginProps["ip"] == matrix.ip:
				selMatrix = matrix

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

		for matrix in self.matrixList:
			if matrixdev.pluginProps["ip"] == matrix.ip:
				selMatrix = matrix

		for TxDev in [s for s in indigo.devices.iter(filter="self.transmitter") if s.enabled]:
			for Tx in selMatrix.Tx:
				if TxDev.pluginProps["ip"] == Tx.ip:
					value = TxDev.id
					text = TxDev.name

					availableTx.append((value, text))

		return availableTx

	def updateDevices(self):
		for matrix in self.matrixList:
			for dev in indigo.devices.iter("com.perceptiveautomation.indigoplugin.justaddpower"):
				if dev.pluginProps["ip"] == matrix.ip:
					matrix_dev = dev
					break

			for Rx in matrix.Rx:
				devExists = False
				devName = ""

				if len(self.matrixList) > 1:
					devName = matrix_dev.name + " Rx. " + str(Rx.no)
				else:
					devName = "JAP Rx. " + str(Rx.no)

				devAddr = Rx.ip
				devDesc = devName

				for dev in indigo.devices.iter("com.perceptiveautomation.indigoplugin.justaddpower"):
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
						pluginId="com.perceptiveautomation.indigoplugin.justaddpower",
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

				for dev in indigo.devices.iter("com.perceptiveautomation.indigoplugin.justaddpower"):
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
						pluginId="com.perceptiveautomation.indigoplugin.justaddpower",
						deviceTypeId="transmitter",
						folder=self.indigoDeviceFolderID
					)
				
					dev = indigo.devices[devName]
					props = dev.pluginProps

					props["matrix"] = matrix_dev.id
					props["ip"] = Tx.ip
					props["vlan"] = Tx.vlan

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

	def updateVariables(self):
		self.logger.debug("started variable updates")
		if self.indigoVariablesFolderID is not None:
			for dev in [s for s in indigo.devices.iter(filter="self") if s.enabled]:

				if dev.deviceTypeId != "matrix":
					selMatrix = None
					for matrix in self.matrixList:
						if dev.pluginProps["matrix"] == matrix.ip:
							selMatrix = matrix

					varName = dev.name.replace(' ', '_') + "_image_pull_url"
					if not varName in indigo.variables:
						self.logger.debug("Created variables for device " + dev.name)
						indigo.variable.create(varName,folder=self.indigoVariablesFolderID)

					varValue = "http://" + dev.pluginProps["ip"] + "/pull.bmp"

					if indigo.variables[varName].value != varValue:
						self.logger.debug("Updated variable value for device " + dev.name)
						indigo.variable.updateValue(varName, varValue)

		else:
			self.createVariableFolder(self.indigoVariablesFolderName)

		self.logger.debug("finished variable updates")

