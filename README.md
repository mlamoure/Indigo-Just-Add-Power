Indigo (http://www.indigodomo.com) plugin for Just Add Power (www.justaddpower.com) HDMI over IP Distribution Systems

# Requirements #
JAP Matrix with a Cisco Switch.  Luxul switches are not supported yet.

Supports justOS firmware versions.  Legacy "A" firmware is still available but unsupported since I have no way of testing it.

# Features #
* Automatically detects Rx's and Tx's configured in your JAP matrix (though standard JADConfig setup)
* Updates the state of each Rx and Tx.  Rx's display which VLAN they are on, and which Tx device is assigned to that VLAN.  Tx's display which Rx's are watching that source.  See the state table below.
* Action for switching a Rx to a different Transmitter (VLAN)
* Loads parts of the configuration of a Rx and Tx, such as the current Image Pull status
* Action for enabling and disabling image pull on a Tx or Rx
* Support for "image pull client" where Indigo will download the image pull images for each Rx and Tx, convert them to JPEG, and store them into a working directory.  These are then available for embedding into a Control Page.
* Action for rebooting a switch
* Action for rebooting a JAP device
* A maintained list of Indigo variables with the URL to obtain a image pull from each Tx and Rx

# Install Notes #
* No plugin config needed (debug mode only for troubleshooting)
* Add your switch using the standard "New Device" method.  Put in the IP, login, password of your switch.  Change the Control VLAN only if it is not the default.
* No need to add your individual Tx's and Rx's, the plugin will do that for you.  Feel free to rename the Tx's and Rx's to something more descriptive, devices are managed by the plugin through their IP address, which shouldn't change.

# States #

| State                | Type    | Description                                                                                                                                                                                                                  |
|:---------------------|:--------|:-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| vlan_watching       | String  | (Rx only) Contains the VLAN no. of the Tx that the Receiver is watching.                                                                                       |
| being_watched       | String  | (Tx only) Contains a list of Rx numbers that are watching that transmitter, separated by commas.                                                                                       |
| image_pull_enabled  | Boolean | (Both Rx and Tx) Lets you know if Image Pull is enabled on that Rx or Tx                                                                                            |

# Limitations #
* Luxul switch support not yet implemented

# Untested but supported #
* Multiple switches
* Cisco SG500 switch