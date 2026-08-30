from protoboard.geometry import Point, Vector, Vector3D


def _ve(*args) -> Vector3D | Vector:
    if len(args) == 3:
        return Vector3D(args[0], args[1], args[2])

    if len(args) < 3:
        return Vector(
            args[0] if len(args) >= 1 else 0.0, args[1] if len(args) >= 2 else 0.0
        )
    raise ValueError(f"Invalid number of arguments for vector: '{args}'")


def _pt(x: float, y: float) -> Point:
    return Point(float(x), float(y))
