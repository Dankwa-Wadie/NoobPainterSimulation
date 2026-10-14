"""Blender side of carving.py: render height maps as a carved relief.

Run by Blender, not Python:  blender -b --factory-startup -P relief_scene.py -- job.json

The job lists height-map PNGs (16-bit, white = high). Each is rendered as a
slab: a finely subdivided plane displaced by the map, a wood, marble or clay
material, and a low raking light from the upper left so every cut catches a
shadow. The camera is orthographic and square to the slab, so a point on the
height map lands on the same point of the frame (the playback's hand relies on
it).
"""
import json
import sys
from pathlib import Path

import bpy

job = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text(encoding="utf-8"))
W, H = job["size"]                      # frame size, px
aspect = W / H
depth = float(job.get("depth", 0.18))   # relief height, in slab widths

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
for engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
    try:
        scene.render.engine = engine
        break
    except TypeError:
        continue
scene.render.resolution_x, scene.render.resolution_y = W, H
scene.render.image_settings.file_format = "PNG"
scene.render.film_transparent = False
eevee = scene.eevee
for name, value in (("taa_render_samples", 32), ("use_shadows", True), ("use_gtao", True)):
    if hasattr(eevee, name):
        setattr(eevee, name, value)

# --- the slab: 2 wide, 2/aspect tall, displaced by the height image ----------
bpy.ops.mesh.primitive_plane_add(size=2)
slab = bpy.context.active_object
slab.scale = (1, 1 / aspect, 1)
bpy.ops.object.transform_apply(scale=True)
sub = slab.modifiers.new("fine", "SUBSURF")
sub.subdivision_type = "SIMPLE"
sub.levels = sub.render_levels = int(job.get("subdiv", 9))
image = bpy.data.images.load(job["heights"][0])
image.colorspace_settings.name = "Non-Color"
tex = bpy.data.textures.new("height", "IMAGE")
tex.image = image
tex.extension = "EXTEND"
disp = slab.modifiers.new("carve", "DISPLACE")
disp.texture = tex
disp.texture_coords = "UV"
disp.strength = depth * 2
disp.mid_level = 0.0
bpy.ops.object.shade_smooth()

# --- material ----------------------------------------------------------------
mat = bpy.data.materials.new("material")
mat.use_nodes = True
nodes, links = mat.node_tree.nodes, mat.node_tree.links
bsdf = next(n for n in nodes if n.type == "BSDF_PRINCIPLED")
kind = job.get("material", "wood")
material_light: dict = {}                # a material's own light, unless the job says otherwise
if kind == "wood":
    wave = nodes.new("ShaderNodeTexWave")
    # fine, quiet grain: bold stripes fought the carving for attention
    wave.inputs["Scale"].default_value = 9.0
    wave.inputs["Distortion"].default_value = 3.0
    wave.inputs["Detail"].default_value = 2.0
    ramp = nodes.new("ShaderNodeValToRGB")
    grain = float(job.get("grain", 0.4))           # 0 plain .. 1 the full contrast
    dark, light = (0.52, 0.34, 0.18), (0.58, 0.38, 0.21)
    ramp.color_ramp.elements[0].color = (*[l - (l - d) * grain * 2 for d, l in zip(dark, light)], 1)
    ramp.color_ramp.elements[1].color = (*light, 1)
    links.new(wave.outputs["Fac"], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.55
elif kind == "marble":
    # warm white with faint grey veins, light glowing slightly into the stone
    # (cold grey and opaque, it read as plaster or aluminium)
    # Veins: the edges of warped Voronoi cells, thin and branching (a wave texture
    # made blotches like stains). Colours are linear: sRGB-looking numbers rendered
    # everything paler and cooler than meant.
    coord = nodes.new("ShaderNodeTexCoord")
    warp = nodes.new("ShaderNodeTexNoise")
    warp.inputs["Scale"].default_value = 1.8
    warp.inputs["Detail"].default_value = 5.0
    mix = nodes.new("ShaderNodeVectorMath")
    mix.operation = "MULTIPLY_ADD"
    mix.inputs[1].default_value = (1.3, 1.3, 1.3)    # strongly warped: no polygon network
    links.new(warp.outputs["Color"], mix.inputs[0])
    links.new(coord.outputs["Object"], mix.inputs[2])
    cells = nodes.new("ShaderNodeTexVoronoi")
    cells.feature = "DISTANCE_TO_EDGE"
    cells.inputs["Scale"].default_value = 1.3
    links.new(mix.outputs["Vector"], cells.inputs["Vector"])
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.0
    ramp.color_ramp.elements[0].color = (0.58, 0.56, 0.53, 1)       # the vein, faint
    ramp.color_ramp.elements[1].position = 0.022
    ramp.color_ramp.elements[1].color = (0.86, 0.82, 0.74, 1)       # warm white stone
    links.new(cells.outputs["Distance"], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.3
    for name, value in (("Subsurface Weight", 0.25), ("Subsurface", 0.25)):
        if name in bsdf.inputs:
            bsdf.inputs[name].default_value = value
            break
    if "Subsurface Radius" in bsdf.inputs:
        bsdf.inputs["Subsurface Radius"].default_value = (0.05, 0.04, 0.03)
    material_light = {"ambient": 0.24, "sun": 4.6}
else:                                    # clay: fired terracotta, matte, faintly mottled and grainy
    # (flat salmon, it looked like pink plastic)
    mottle = nodes.new("ShaderNodeTexNoise")
    mottle.inputs["Scale"].default_value = 6.0
    mottle.inputs["Detail"].default_value = 4.0
    ramp = nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (0.33, 0.085, 0.035, 1)     # terracotta, in linear light
    ramp.color_ramp.elements[1].color = (0.45, 0.13, 0.055, 1)
    links.new(mottle.outputs["Fac"], ramp.inputs["Fac"])
    links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.92
    grain = nodes.new("ShaderNodeTexNoise")
    grain.inputs["Scale"].default_value = 600.0
    bump = nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.08
    links.new(grain.outputs["Fac"], bump.inputs["Height"])
    links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    material_light = {"ambient": 0.14, "sun": 5.0}
slab.data.materials.append(mat)

# --- light and camera ---------------------------------------------------------
# Low and hard, from the upper left: the tilt is from straight down (1.25 rad is
# ~18 degrees above the slab). At 1.05 and a soft sun the full-size render lost
# its shadows and the finished relief looked flat.
bpy.ops.object.light_add(type="SUN", rotation=(0.0, float(job.get("sun_tilt", 1.3)), 2.35))
sun = bpy.context.active_object
sun.data.energy = float(job.get("sun", material_light.get("sun", 5.5)))
sun.data.angle = float(job.get("sun_softness", 0.01))
if hasattr(sun.data, "shadow_maximum_resolution"):   # Eevee Next: crisp shadow maps
    sun.data.shadow_maximum_resolution = 0.001
world = bpy.data.worlds.new("world")
scene.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Strength"].default_value = float(job.get("ambient", material_light.get("ambient", 0.10)))
bpy.ops.object.camera_add(location=(0, 0, 5), rotation=(0, 0, 0))
cam = bpy.context.active_object
cam.data.type = "ORTHO"
cam.data.ortho_scale = max(2.0, 2.0 / aspect)     # the slab's longer side fills the frame
scene.camera = cam
scene.view_settings.view_transform = "Standard"

# --- render every height map --------------------------------------------------
out = Path(job["out_dir"])
out.mkdir(parents=True, exist_ok=True)
for i, path in enumerate(job["heights"]):
    if i:
        image.filepath = path
        image.reload()
    scene.render.filepath = str(out / f"{i + 1:05d}.png")
    bpy.ops.render.render(write_still=True)
    print(f"RENDERED {i + 1}/{len(job['heights'])}", flush=True)
