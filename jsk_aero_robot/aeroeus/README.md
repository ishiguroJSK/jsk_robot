# aeroeus

This is EUSLISP interface to control aero.

## Build aeroeus

```
catkin build aeroeus  # nothing to build, to recognize from rospack
source ~/.bashrc
```

## Run euslisp

```
roscd aeroeus
roseus aero-interface.l
```

To initialize eus interface,

```
(aero-init)
(load-controllers)
(objects (list *aero*))
```

Then, you can control AERO from euslisp, like

```
(send *aero* :reset-manip-pose)
(send *ri* :angle-vector (send *aero* :angle-vector) 5000)
```
