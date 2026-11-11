"""Import-time source-cuboid opening limits and native continuous joints."""

import json
import math
from dataclasses import dataclass

from mathutils import Matrix, Vector

from .asset_effects import native


def model_matrix(obj, root):
    if obj is None or obj == root:
        return Matrix.Identity(4)
    return model_matrix(obj.parent,root) @ obj.matrix_parent_inverse @ obj.matrix_basis


@dataclass(frozen=True)
class Box:
    center: Vector
    axes: tuple
    radius: tuple

    @classmethod
    def enclosing(cls, points, orientation):
        axes = tuple(orientation.col[i].normalized() for i in range(3))
        intervals = tuple((min(p.dot(axis) for p in points),max(p.dot(axis) for p in points)) for axis in axes)
        return cls(sum((axis*((lo+hi)/2) for axis,(lo,hi) in zip(axes,intervals,strict=True)),Vector()),axes,
                   tuple((hi-lo)/2 for lo,hi in intervals))

    def overlap(self, other):
        delta = other.center-self.center
        candidates = [*self.axes,*other.axes]
        candidates.extend(cross.normalized() for first in self.axes for second in other.axes if (cross := first.cross(second)).length_squared > 1e-12)
        depth = math.inf
        for axis in candidates:
            extent = sum(abs(axis.dot(direction))*radius for box in (self,other) for direction,radius in zip(box.axes,box.radius,strict=True))
            overlap = extent-abs(delta.dot(axis))
            if overlap <= 1e-7:
                return 0.
            depth = min(depth,overlap)
        return depth


def _geometry(part,root):
    return [(obj,[model_matrix(obj,root) @ vertex.co for vertex in obj.data.vertices])
            for obj in tuple(part.children_recursive) if obj.type == "MESH"]


def _rebase(part,root,pivot,basis):
    previous = model_matrix(part,root)
    next_matrix = Matrix.Translation(pivot) @ basis.to_4x4()
    for obj in tuple(part.children):
        obj.matrix_basis = next_matrix.inverted() @ previous @ obj.matrix_parent_inverse @ obj.matrix_basis
        obj.matrix_parent_inverse = Matrix.Identity(4)
    part.matrix_parent_inverse = Matrix.Identity(4)
    part.matrix_basis = model_matrix(part.parent,root).inverted() @ next_matrix


def chest_motion(root,parts,collection,*,lid_name="lid",lock_name="lock",bottom_name="bottom",correct_hinge=False):
    lid = parts[lid_name]
    lock = parts.get(lock_name)
    points = [p for _,values in _geometry(lid,root) for p in values]
    if not points:
        raise ValueError("箱盖资源没有原生几何")
    if correct_hinge:
        body = [p for _,values in _geometry(parts[bottom_name],root) for p in values]
        lock_points = [p for _,values in _geometry(lock,root) for p in values]
        center = sum(body,Vector())/len(body)
        front = sum(lock_points,Vector())/len(lock_points)-center
        front.z = 0
        if front.length < 1e-6:
            raise ValueError("箱体与锁扣不足以确定原版铰链")
        front.normalize()
        back_distance = min((point-center).dot(front) for point in body)
        pivot = center+front*back_distance
        pivot.z = min(point.z for point in points)
        axis = Vector((front.y,-front.x,0))
        basis = Matrix(((axis.x,axis.y,axis.z),(front.x,front.y,front.z),(0.,0.,1.))).transposed()
        _rebase(lid,root,pivot,basis)
        if lock is not None:
            _rebase(lock,root,pivot,basis)
    matrix = model_matrix(lid,root)
    pivot,basis = matrix.translation,matrix.to_3x3()
    axis = basis.col[0].normalized()
    sign = 1 if axis.cross(sum(points,Vector())/len(points)-pivot).z > 0 else -1
    moving = Box.enclosing(points,basis)
    obstacles = []
    moving_objects = {obj for obj,_ in _geometry(lid,root)} | ({obj for obj,_ in _geometry(lock,root)} if lock is not None else set())
    for obj in tuple(collection.objects):
        if obj.type != "MESH" or obj in moving_objects or obj.get("oxal_mc_studio_effect_geometry"):
            continue
        transform = model_matrix(obj,root)
        spans = json.loads(obj.data.get("oxal_mc_studio_cuboids","null")) or [(0,len(obj.data.vertices))]
        for start,end in spans:
            values = [transform @ obj.data.vertices[i].co for i in range(start,end)]
            if not values:
                continue
            box = Box.enclosing(values,transform.to_3x3())
            if min(box.radius)>1e-5:
                obstacles.append(box)
    baseline = [moving.overlap(box) for box in obstacles]
    maximum,collision = 179.,None
    for degrees in range(1,180):
        rotation = Matrix.Rotation(math.radians(sign*degrees),3,axis)
        rotated = Box.enclosing([pivot+rotation@(point-pivot) for point in points],rotation@basis)
        if any(rotated.overlap(box)>max(previous,.002)+.002 for box,previous in zip(obstacles,baseline,strict=True)):
            maximum,collision = float(max(1,degrees-1)),degrees
            break
    root["open"] = 0.
    root["自动开合"],root["最大开盖角度"] = True,maximum
    root["箱盖限角检查"] = json.dumps({"search_endpoint_degrees":179,"first_collision_degrees":collision,"maximum_degrees":maximum,"obstacle_boxes":len(obstacles),"baseline_penetrations":baseline,"tolerance":.002})
    root.id_properties_ui("最大开盖角度").update(min=0,max=maximum)
    for part in (lid,lock):
        if part is not None:
            rest = float(part.rotation_euler.x)
            native(part,"rotation_euler",f"{rest}+{sign}*a*p*m*pi/180",root,
                   [("a","自动开合"),("p","open"),("m","最大开盖角度")],0)
