Indigo (http://www.indigodomo.com) plugin for Just Add Power (www.justaddpower.com) Devices

# Requirements #
JAP Matrix with a Cisco Switch.  Luxul switches are not supported yet.

# Features #
* Automatically detects Rx's and Tx's configured in your JAP matrix (though standard JADConfig setup)
* Updates the state of each Rx and Tx.  Rx's display which VLAN they are on, and which Tx device is assigned to that VLAN.  Tx's display which Rx's are watching that source.  See the state table below.
* Action for enabling and disabling image pull on a Tx or Rx
* Action for rebooting a switch
* Action for rebotting a JAP device
* A maintained list of Indigo variables with the URL to obtain a image pull from each Tx and Rx

# Install Notes #
* No plugin config needed (debug mode only for troubleshooting)
* Add your switch using the standard "New Device" method
* No need to add your individual Tx's and Rx's, the plugin will do that for you.  Feel free to rename the Tx's and Rx's to something more descriptive, devices are managed by the plugin through their IP address, which shouldn't change.

# States #

| State                | Type    | Description                                                                                                                                                                                                                  |
|:---------------------|:--------|:-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| vlan_watching       | String  | (Rx only) Contains the VLAN no. of the Tx that the Receiver is watching.                                                                                       |
| being_watched       | String  | (Tx only) Contains a list of Rx numbers that are watching that transmitter, seperated by commas.                                                                                       |

# Limitations #
* Doesn't work with Luxul switches

# Untested but supported #
* Multiple switches
* Cisco SG500 switch