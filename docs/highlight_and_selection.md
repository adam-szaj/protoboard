# Select and Highlight

## Select
### Component
- select components (components that are in the same nets - excluding global nets like GND, VCC...)
- select components from only one of networks (that components is a member)
- select wires and pins from all networks (that components is a member)
- select wires and pins from one of networks (that components is a member)

### Wire
- select one fragment
- select to the closest pin/joint
- select all continuously connected (electrical) wires
- select wires from the same network
- select connected components
- select components from the same network as wire (not only connected one)

## Highlight

### Normal
In normal mode item of interest are emphasized, Highlighted and/or bold frame/outline.
Items on the bottom side (from camera perspective) may need to be emphasized differently,
compared to items on the top

### Dimmed #1
In dimmed #1 other items are dimmed (darker) but still well visible (maybe slightly lower opacity).

### Dimmed #2
In dimmed #1 other items are dimmed (darker) but with way lower opacity.


## Appendix

Idea behind these different modes is that depending on current task/tool different context is needed to be visible.
Like, when moving component or routing you want to see where are other components from the same nets,
but you also have to see where others are to see which spots are occupied (and by what: components or wires)

For drag, rotate and routing "Dimmed #1" profile has to be selected.

In all cases when pins and wires are highlighted connected to current item (clicked/selected) are highlighted in green,
unconnected in orange.

Highlight profile "Dimmed #2" should be applied when selecting multiple items using commands listed in "Select" section. 
Highlight can be changed anytime by user.
