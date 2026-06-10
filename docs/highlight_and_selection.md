# Selection and highlight — user-facing description

This page sketches the *user-facing* behaviour: what kinds of selection
gestures are possible, and what visual treatment items receive while
selected or highlighted. The canonical FSM that implements all of this
is [highlight-state-machine.txt](highlight-state-machine.txt) (currently
revision 3).

## Selection

### From a component

- Select components that share a net with this one (excluding global
  nets such as GND, VCC…).
- Select components that share *one specific* net with this one.
- Select wires and pins from every net the component is a member of.
- Select wires and pins from one specific net.

### From a wire

- Select one fragment.
- Select up to the nearest pin or joint.
- Select every electrically connected wire (the whole physical net).
- Select every wire of the same logical net.
- Select connected components.
- Select all components of the same net (not only the directly
  connected ones).

## Highlight modes

### Normal

Items of interest are emphasised — highlighted and / or given a bold
frame / outline. Items on the bottom side (from the camera's
perspective) may need a different visual treatment from those on the top.

### Dimmed #1

The other items stay clearly visible but are dimmed a little (slightly
lower opacity). Use this while dragging, rotating or routing — you want
to see where other same-net components are *and* where free spots are.

### Dimmed #2

Other items are dimmed much further (substantially lower opacity). Use
this when selecting many items via the "select all wires of this net"
style commands — focus is on the selected set.

## Conventions

Connected items are highlighted **green**, unconnected items **orange**
(applies to pins and wires when the current item drives the highlight).

The highlight profile can be changed by the user at any time. Drag,
rotate and routing default to *Dimmed #1*; multi-net selection commands
default to *Dimmed #2*.
