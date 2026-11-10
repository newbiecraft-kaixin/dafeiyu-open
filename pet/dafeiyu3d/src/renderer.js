import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";

const pet = window.pet;
const stage = document.getElementById("stage");
const bubbleEl = document.getElementById("bubble");
const bubbleTextEl = document.getElementById("bubbleText");
const bubbleMetaEl = document.getElementById("bubbleMeta");
const zzzEl = document.getElementById("zzz");
const menuEl = document.getElementById("menu");
const hintEl = document.getElementById("hint");
const cfg = (pet && pet.config && pet.config()) || { petName: "大肥鱼" };

/* ------------------------------------------------------------------ */
/* three.js 场景                                                       */
/* ------------------------------------------------------------------ */

const renderer = new THREE.WebGLRenderer({
  alpha: true,
  antialias: true,
  premultipliedAlpha: true,
  preserveDrawingBuffer: true,
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.NoToneMapping;
renderer.domElement.style.width = "100%";
renderer.domElement.style.height = "100%";
stage.appendChild(renderer.domElement);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(34, window.innerWidth / window.innerHeight, 0.05, 20);
camera.position.set(0, 0.03, 2.06);
camera.lookAt(0, -0.01, 0);

const hemi = new THREE.HemisphereLight(0xe8f3ff, 0x3a4468, 2.1);
scene.add(hemi);
const keyLight = new THREE.DirectionalLight(0xffffff, 1.9);
keyLight.position.set(1.2, 2.4, 2.6);
scene.add(keyLight);
const rimLight = new THREE.DirectionalLight(0x8db7ff, 1.1);
rimLight.position.set(-1.8, 1.3, -1.4);
scene.add(rimLight);
const fillLight = new THREE.DirectionalLight(0xffe9f3, 0.45);
fillLight.position.set(0.6, 0.4, 2.2);
scene.add(fillLight);

const lookTarget = new THREE.Object3D();
lookTarget.position.set(0, 0.45, 1.4);
scene.add(lookTarget);

function blobShadowTexture() {
  const size = 256;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  const grad = ctx.createRadialGradient(size / 2, size / 2, 8, size / 2, size / 2, size / 2);
  grad.addColorStop(0, "rgba(20, 42, 82, 0.55)");
  grad.addColorStop(0.55, "rgba(28, 60, 110, 0.22)");
  grad.addColorStop(1, "rgba(30, 70, 130, 0)");
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

const shadow = new THREE.Mesh(
  new THREE.PlaneGeometry(1.0, 0.5),
  new THREE.MeshBasicMaterial({ map: blobShadowTexture(), transparent: true, depthWrite: false })
);
shadow.rotation.x = -Math.PI / 2;
shadow.position.set(0, -0.4, 0.02);
scene.add(shadow);

function bubbleSpriteTexture() {
  const size = 64;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.strokeStyle = "rgba(210, 235, 255, 0.9)";
  ctx.lineWidth = 2.2;
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 4, 0, Math.PI * 2);
  ctx.stroke();
  ctx.fillStyle = "rgba(180, 220, 255, 0.16)";
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 5, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "rgba(255, 255, 255, 0.85)";
  ctx.beginPath();
  ctx.arc(size / 2 - 8, size / 2 - 10, 5, 0, Math.PI * 2);
  ctx.fill();
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

const bubbleTex = bubbleSpriteTexture();
const bubbles = [];
for (let i = 0; i < 9; i += 1) {
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({
      map: bubbleTex,
      transparent: true,
      opacity: 0.55,
      depthWrite: false,
    })
  );
  const scale = 0.035 + Math.random() * 0.05;
  sprite.scale.set(scale, scale, 1);
  sprite.position.set(
    (Math.random() - 0.5) * 0.85,
    -0.35 + Math.random() * 0.9,
    (Math.random() - 0.2) * 0.4
  );
  sprite.userData = { speed: 0.05 + Math.random() * 0.08, wobble: Math.random() * 6.28 };
  bubbles.push(sprite);
  scene.add(sprite);
}

/* 戳一下冒出的心心 */
function heartSpriteTexture() {
  const size = 64;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.translate(size / 2, size / 2 + 4);
  ctx.scale(1, 1);
  ctx.beginPath();
  ctx.moveTo(0, 18);
  ctx.bezierCurveTo(-30, -6, -22, -30, 0, -12);
  ctx.bezierCurveTo(22, -30, 30, -6, 0, 18);
  ctx.closePath();
  ctx.fillStyle = "#ff8fc0";
  ctx.fill();
  ctx.strokeStyle = "rgba(255, 255, 255, 0.85)";
  ctx.lineWidth = 3;
  ctx.stroke();
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

const heartTex = heartSpriteTexture();
const hearts = [];

function spawnHearts(count = 6) {
  if (!headBone) return;
  const origin = new THREE.Vector3();
  headBone.getWorldPosition(origin);
  origin.y += 0.1;
  for (let i = 0; i < count; i += 1) {
    const sprite = new THREE.Sprite(
      new THREE.SpriteMaterial({ map: heartTex, transparent: true, opacity: 0.95, depthWrite: false })
    );
    const size = 0.045 + Math.random() * 0.035;
    sprite.scale.set(size, size, 1);
    sprite.position.set(
      origin.x + (Math.random() - 0.5) * 0.18,
      origin.y + Math.random() * 0.05,
      origin.z + 0.05
    );
    sprite.userData = {
      vx: (Math.random() - 0.5) * 0.22,
      vy: 0.16 + Math.random() * 0.16,
      life: 0,
      maxLife: 1.1 + Math.random() * 0.5,
    };
    hearts.push(sprite);
    scene.add(sprite);
  }
}

function updateHearts(dt) {
  for (let i = hearts.length - 1; i >= 0; i -= 1) {
    const h = hearts[i];
    h.userData.life += dt;
    h.position.x += h.userData.vx * dt;
    h.position.y += h.userData.vy * dt;
    h.material.opacity = Math.max(0, 1 - h.userData.life / h.userData.maxLife);
    if (h.userData.life >= h.userData.maxLife) {
      scene.remove(h);
      h.material.dispose();
      hearts.splice(i, 1);
    }
  }
}


/* ------------------------------------------------------------------ */
/* 配色 / 小配饰                                                       */
/* ------------------------------------------------------------------ */

const COLOR = {
  hair: new THREE.Color(0x4a63e8),
  cloth: new THREE.Color(0x121c44),
  iris: new THREE.Color(0x2f6ee8),
  whaleBody: new THREE.Color(0x5d92e0),
  whaleBelly: new THREE.Color(0xecf5ff),
  accent: new THREE.Color(0x8fc0ff),
};

function tintTexture(mat, hex) {
  const tex = mat.map;
  if (!tex || !tex.image) return false;
  try {
    const img = tex.image;
    const w = img.width;
    const h = img.height;
    if (!w || !h) return false;
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0, w, h);
    ctx.globalCompositeOperation = "color";
    ctx.fillStyle = hex;
    ctx.fillRect(0, 0, w, h);
    ctx.globalCompositeOperation = "source-over";

    const next = new THREE.CanvasTexture(canvas);
    next.colorSpace = THREE.SRGBColorSpace;
    next.flipY = tex.flipY;
    next.wrapS = tex.wrapS;
    next.wrapT = tex.wrapT;
    next.magFilter = tex.magFilter;
    next.minFilter = tex.minFilter;
    next.anisotropy = tex.anisotropy;
    next.needsUpdate = true;
    mat.map = next;
    return true;
  } catch (err) {
    return false;
  }
}

function recolor(vrm) {
  vrm.scene.traverse((obj) => {
    if (!obj.isMesh) return;
    const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
    mats.forEach((mat) => {
      if (!mat || !mat.color) return;
      const tag = `${mat.name || ""} ${obj.name || ""}`;
      if (/HAIR/i.test(tag)) {
        const ok = tintTexture(mat, "#4a63e8");
        if (ok) mat.color.set(0xffffff);
        else mat.color.setRGB(0.34, 0.46, 1.5);
        if ("shadeColorFactor" in mat) mat.shadeColorFactor = new THREE.Color(0x3346b8);
      } else if (/TOPS|BOTTOMS/i.test(tag)) {
        mat.color.set(0x121c44);
        if ("shadeColorFactor" in mat) mat.shadeColorFactor = new THREE.Color(0x080d24);
      } else if (/SHOES/i.test(tag)) {
        mat.color.set(0x1a2450);
      } else if (/EyeIris/i.test(tag)) {
        const ok = tintTexture(mat, "#2f6ee8");
        if (ok) mat.color.set(0xffffff);
        else mat.color.setRGB(0.2, 0.45, 1.2);
      } else if (/EyeHighlight/i.test(tag)) {
        mat.color.set(0xffffff);
      }
      mat.needsUpdate = true;
    });
  });
}

function createWhalePlush() {
  const group = new THREE.Group();
  const bodyMat = new THREE.MeshStandardMaterial({ color: COLOR.whaleBody, roughness: 0.62 });
  const bellyMat = new THREE.MeshStandardMaterial({ color: COLOR.whaleBelly, roughness: 0.75 });
  const darkMat = new THREE.MeshStandardMaterial({ color: 0x23324c, roughness: 0.35 });
  const blushMat = new THREE.MeshStandardMaterial({
    color: 0xff9fc0,
    roughness: 0.9,
    transparent: true,
    opacity: 0.85,
  });

  const body = new THREE.Mesh(new THREE.SphereGeometry(1, 40, 28), bodyMat);
  body.scale.set(1.05, 0.8, 0.9);
  group.add(body);

  const belly = new THREE.Mesh(new THREE.SphereGeometry(1, 32, 22), bellyMat);
  belly.scale.set(0.86, 0.6, 0.62);
  belly.position.set(0, -0.26, 0.42);
  group.add(belly);

  const finGeo = new THREE.SphereGeometry(1, 18, 12);
  [-1, 1].forEach((side) => {
    const fin = new THREE.Mesh(finGeo, bodyMat);
    fin.scale.set(0.42, 0.14, 0.26);
    fin.position.set(side * 0.98, -0.26, 0.12);
    fin.rotation.z = side * 0.45;
    group.add(fin);
  });

  [-1, 1].forEach((side) => {
    const fluke = new THREE.Mesh(finGeo, bodyMat);
    fluke.scale.set(0.5, 0.16, 0.3);
    fluke.position.set(-1.02, 0.24, side * 0.24);
    fluke.rotation.z = 0.25;
    fluke.rotation.y = side * 0.6;
    group.add(fluke);
  });

  [-1, 1].forEach((side) => {
    const eye = new THREE.Mesh(new THREE.SphereGeometry(0.09, 16, 12), darkMat);
    eye.position.set(side * 0.4, 0.11, 0.8);
    group.add(eye);
    const shine = new THREE.Mesh(
      new THREE.SphereGeometry(0.03, 10, 8),
      new THREE.MeshBasicMaterial({ color: 0xffffff })
    );
    shine.position.set(side * 0.4 + 0.025, 0.14, 0.86);
    group.add(shine);

    const blush = new THREE.Mesh(new THREE.CircleGeometry(0.1, 18), blushMat);
    blush.position.set(side * 0.52, -0.06, 0.74);
    blush.lookAt(0, -0.06, 2);
    group.add(blush);
  });

  return group;
}

function createCollarSet() {
  const group = new THREE.Group();
  const laceMat = new THREE.MeshStandardMaterial({ color: 0xf7fbff, roughness: 0.85 });
  const bowMat = new THREE.MeshStandardMaterial({ color: 0x6aa8ff, roughness: 0.5 });

  const collar = new THREE.Mesh(new THREE.TorusGeometry(0.062, 0.009, 10, 32), laceMat);
  collar.rotation.x = Math.PI / 2;
  group.add(collar);

  const knot = new THREE.Mesh(new THREE.SphereGeometry(0.011, 12, 10), bowMat);
  knot.position.set(0, -0.02, 0.055);
  group.add(knot);

  [-1, 1].forEach((side) => {
    const wing = new THREE.Mesh(new THREE.SphereGeometry(1, 14, 10), bowMat);
    wing.scale.set(0.019, 0.011, 0.006);
    wing.position.set(side * 0.024, -0.02, 0.055);
    wing.rotation.z = side * 0.42;
    group.add(wing);
  });

  return group;
}

function apronTexture() {
  const W = 256;
  const H = 320;
  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d");

  ctx.fillStyle = "#fbfdff";
  ctx.beginPath();
  ctx.moveTo(46, 12);
  ctx.quadraticCurveTo(28, 12, 26, 34);
  ctx.lineTo(20, 250);
  ctx.quadraticCurveTo(128, 296, 236, 250);
  ctx.lineTo(230, 34);
  ctx.quadraticCurveTo(228, 12, 210, 12);
  ctx.closePath();
  ctx.fill();

  ctx.fillStyle = "#ffffff";
  for (let x = 20; x <= 236; x += 29) {
    ctx.beginPath();
    ctx.arc(x, 250, 15, 0, Math.PI);
    ctx.fill();
  }

  ctx.strokeStyle = "rgba(178, 208, 240, 0.85)";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(40, 60);
  ctx.lineTo(216, 60);
  ctx.stroke();

  ctx.fillStyle = "#8ec0ff";
  ctx.beginPath();
  ctx.arc(128, 150, 44, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = "#4b83d6";
  ctx.beginPath();
  ctx.ellipse(124, 152, 30, 18, -0.08, 0, Math.PI * 2);
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(150, 145);
  ctx.lineTo(172, 128);
  ctx.lineTo(168, 158);
  ctx.closePath();
  ctx.fill();

  ctx.fillStyle = "#eef6ff";
  ctx.beginPath();
  ctx.ellipse(121, 163, 20, 9, -0.08, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = "#23324c";
  ctx.beginPath();
  ctx.arc(110, 146, 3, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = "#6aa8ff";
  ctx.beginPath();
  ctx.moveTo(128, 46);
  ctx.lineTo(104, 30);
  ctx.lineTo(108, 58);
  ctx.closePath();
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(128, 46);
  ctx.lineTo(152, 30);
  ctx.lineTo(148, 58);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = "#ffffff";
  ctx.beginPath();
  ctx.arc(128, 46, 7, 0, Math.PI * 2);
  ctx.fill();

  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function createApron() {
  const mesh = new THREE.Mesh(
    new THREE.PlaneGeometry(0.2, 0.25, 1, 1),
    new THREE.MeshBasicMaterial({
      map: apronTexture(),
      transparent: true,
      depthWrite: false,
      side: THREE.DoubleSide,
    })
  );
  return mesh;
}

function createWhaleClip() {  const group = new THREE.Group();
  const finMat = new THREE.MeshStandardMaterial({
    color: 0x5d92e0,
    roughness: 0.45,
    side: THREE.DoubleSide,
  });

  const shape = new THREE.Shape();
  shape.moveTo(0, 0);
  shape.lineTo(0.16, 0.34);
  shape.quadraticCurveTo(0.28, 0.12, 0.19, -0.05);
  shape.lineTo(0, 0);
  const geo = new THREE.ExtrudeGeometry(shape, {
    depth: 0.045,
    bevelEnabled: true,
    bevelSize: 0.012,
    bevelThickness: 0.012,
    bevelSegments: 2,
    curveSegments: 10,
  });

  const left = new THREE.Mesh(geo, finMat);
  left.rotation.z = 0.42;
  group.add(left);

  const right = new THREE.Mesh(geo, finMat);
  right.scale.x = -1;
  right.position.x = 0.09;
  right.rotation.z = 0.42;
  group.add(right);

  const gem = new THREE.Mesh(
    new THREE.SphereGeometry(0.045, 14, 10),
    new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.2 })
  );
  gem.position.set(0.045, 0.04, 0.02);
  group.add(gem);

  return group;
}

/* ------------------------------------------------------------------ */
/* VRM 加载                                                            */
/* ------------------------------------------------------------------ */

let vrm = null;
let plush = null;
let clip = null;
let collarSet = null;
let apronMesh = null;
let headBone = null;
let chestBone = null;
let neckBone = null;
let hipsBone = null;
const restRot = {};
const bones = {};
let ready = false;
let modelBaseY = 0;

function cacheRestRot(name) {
  const node = vrm.humanoid.getNormalizedBoneNode(name);
  if (!node) return null;
  bones[name] = node;
  restRot[name] = node.rotation.clone();
  return node;
}

const loader = new GLTFLoader();
loader.register((parser) => new VRMLoaderPlugin(parser));

loader.load(
  "pet://app/assets/avatar_sample.vrm",
  (gltf) => {
    const loaded = gltf.userData.vrm;
    if (!loaded) {
      console.error("[pet] VRM plugin missing");
      return;
    }
    vrm = loaded;

    VRMUtils.removeUnnecessaryVertices(gltf.scene);
    VRMUtils.combineSkeletons(gltf.scene);
    vrm.scene.traverse((obj) => {
      obj.frustumCulled = false;
    });

    recolor(vrm);

    const box = new THREE.Box3().setFromObject(vrm.scene);
    const size = box.getSize(new THREE.Vector3());
    const targetHeight = 0.9;
    const s = targetHeight / Math.max(size.y, 0.0001);
    vrm.scene.scale.setScalar(s);

    const box2 = new THREE.Box3().setFromObject(vrm.scene);
    const center = box2.getCenter(new THREE.Vector3());
    vrm.scene.position.x -= center.x;
    vrm.scene.position.z -= center.z;
    vrm.scene.position.y += -0.4 - box2.min.y;
    modelBaseY = vrm.scene.position.y;

    scene.add(vrm.scene);

    if (vrm.lookAt) {
      vrm.lookAt.target = lookTarget;
      vrm.lookAt.autoUpdate = true;
    }

    [
      "hips",
      "spine",
      "chest",
      "neck",
      "head",
      "leftUpperArm",
      "leftLowerArm",
      "leftHand",
      "rightUpperArm",
      "rightLowerArm",
      "rightHand",
      "leftUpperLeg",
      "rightUpperLeg",
    ].forEach(cacheRestRot);

    headBone = bones.head ? vrm.humanoid.getRawBoneNode("head") : null;
    chestBone = bones.chest ? vrm.humanoid.getRawBoneNode("chest") : null;
    neckBone = vrm.humanoid.getRawBoneNode("neck") || headBone;
    hipsBone = vrm.humanoid.getRawBoneNode("hips") || chestBone;

    collarSet = createCollarSet();
    scene.add(collarSet);

    apronMesh = createApron();
    scene.add(apronMesh);

    plush = createWhalePlush();
    plush.scale.setScalar(0.055);
    scene.add(plush);

    clip = createWhaleClip();
    clip.scale.setScalar(0.06);
    scene.add(clip);

    ready = true;
    document.body.classList.add("ready");
  },
  undefined,
  (err) => {
    console.error("[pet] VRM load failed", err);
    const tip = document.createElement("div");
    tip.style.cssText =
      "position:absolute;left:50%;top:40%;transform:translate(-50%,-50%);color:#dce9ff;font-size:12px;text-align:center";
    tip.textContent = "模型加载失败，请查看控制台";
    stage.appendChild(tip);
  }
);

/* ------------------------------------------------------------------ */
/* 状态 + 动画                                                         */
/* ------------------------------------------------------------------ */

let activeState = "idle";
let activeSource = "local";
let blinkValue = 0;
let nextBlinkAt = 2.5;
let blinkStartedAt = -1;
const t0 = performance.now();
const clock = new THREE.Clock();

const SMOOTH = 0.09;

let actionName = "";
let actionStartedAt = 0;
let actionDuration = 1.6;
let lastPointerAt = performance.now();
const ACTIONS = new Set(["wave", "hop", "shy", "nod", "shake", "spin"]);

function playAction(name, duration = 1.6) {
  if (!ACTIONS.has(name)) return;
  actionName = name;
  actionStartedAt = performance.now();
  actionDuration = Math.max(0.4, duration);
}

const pose = {
  headX: 0,
  headY: 0,
  headZ: 0,
  spineX: 0,
  spineZ: 0,
  hipsX: 0,
  armL: -1.22,
  armR: -1.22,
  groupY: 0,
};

function lerpTo(key, target) {
  pose[key] += (target - pose[key]) * SMOOTH;
}

function updateAnimation(dt, t) {
  if (!ready || !vrm) return;
  const state = activeState;

  let target = { headX: 0, headY: 0, headZ: 0, spineX: 0, spineZ: 0, hipsX: 0, armL: 0, armR: 0, groupY: 0 };
  const ARM_DOWN = -1.22;

  const idleSway = Math.sin(t * 0.9);
  const breathe = Math.sin(t * 1.5);

  if (state === "working") {
    target.headX = Math.sin(t * 2.4) * 0.055;
    target.headY = Math.sin(t * 0.7) * 0.14;
    target.headZ = Math.sin(t * 1.1) * 0.035;
    target.spineZ = Math.sin(t * 1.4) * 0.04;
    target.armL = ARM_DOWN + Math.sin(t * 2.0) * 0.07;
    target.armR = ARM_DOWN + Math.sin(t * 2.0 + 1.1) * 0.07;
  } else if (state === "thinking") {
    target.headX = -0.06;
    target.headY = Math.sin(t * 0.5) * 0.2;
    target.headZ = 0.13;
    target.spineZ = Math.sin(t * 0.6) * 0.03;
    target.armL = ARM_DOWN + 0.52;
    target.armR = ARM_DOWN + 0.04;
  } else if (state === "sleeping") {
    target.headX = 0.24;
    target.headZ = 0.14;
    target.spineZ = 0.05;
    target.headY = Math.sin(t * 0.35) * 0.04;
    target.groupY = breathe * 0.006 - 0.008;
  } else if (state === "happy") {
    target.headX = Math.sin(t * 5.5) * 0.06 - 0.03;
    target.headZ = Math.sin(t * 3.2) * 0.06;
    target.armL = ARM_DOWN + 0.2 + Math.sin(t * 5.5) * 0.1;
    target.armR = ARM_DOWN + 0.2 + Math.sin(t * 5.5 + 0.8) * 0.1;
    target.groupY = Math.abs(Math.sin(t * 5.5)) * 0.012;
  } else if (state === "attention") {
    target.headX = -0.05;
    target.headZ = Math.sin(t * 2.2) * 0.05;
    target.armL = ARM_DOWN + 0.5;
    target.armR = ARM_DOWN + 0.5;
    target.groupY = Math.abs(Math.sin(t * 3)) * 0.008;
  } else if (state === "error") {
    target.headX = 0.04;
    target.headZ = Math.sin(t * 8) * 0.08;
    target.spineZ = Math.sin(t * 6) * 0.04;
    target.armL = ARM_DOWN + 0.06 + Math.sin(t * 6) * 0.04;
    target.armR = ARM_DOWN + 0.06 + Math.sin(t * 6 + 0.9) * 0.04;
  } else {
    target.headY = idleSway * 0.08;
    target.headZ = Math.sin(t * 0.62) * 0.03;
    target.headX = breathe * 0.008;
    target.spineZ = idleSway * 0.018;
    target.armL = ARM_DOWN + Math.sin(t * 0.8) * 0.025;
    target.armR = ARM_DOWN + Math.sin(t * 0.8 + 1.2) * 0.025;
    target.groupY = breathe * 0.004;
  }

  /* 空闲时视线自己游走，不一直盯着光标 */
  const idleFor = (performance.now() - lastPointerAt) / 1000;
  if (idleFor > 4) {
    lookTarget.position.set(Math.sin(t * 0.45) * 0.16, 0.4 + Math.sin(t * 0.32) * 0.05, 1.2);
  }

  /* 临时动作：戳一下、打招呼、摇头等，叠加在状态动作上 */
  let actionHappy = 0;
  if (actionName) {
    const p = Math.min(1, (performance.now() - actionStartedAt) / (actionDuration * 1000));
    if (p >= 1) {
      if (actionName === "spin") vrm.scene.rotation.y = 0;
      actionName = "";
    } else {
      const env = Math.sin(p * Math.PI);
      if (actionName === "wave") {
        target.armR = ARM_DOWN + 1.5 + Math.sin(t * 13) * 0.28;
        target.headZ = -0.07;
        target.headY = 0.1 * env;
        actionHappy = 0.6;
      } else if (actionName === "hop") {
        target.groupY += Math.abs(Math.sin(p * Math.PI * 3)) * 0.03 * (1 - p);
        target.headX = -0.05;
        actionHappy = 0.7;
      } else if (actionName === "shy") {
        target.headX = 0.2 * env;
        target.headZ = 0.12 * env;
        target.armL = ARM_DOWN + 0.6 * env;
        target.armR = ARM_DOWN + 0.6 * env;
        actionHappy = 0.4;
      } else if (actionName === "nod") {
        target.headX = Math.sin(p * Math.PI * 6) * 0.09;
      } else if (actionName === "shake") {
        target.headZ = Math.sin(p * Math.PI * 18) * 0.09;
      } else if (actionName === "spin") {
        vrm.scene.rotation.y = p * Math.PI * 2;
      }
    }
  }
  if (actionName !== "spin") {
    vrm.scene.rotation.y += (0 - vrm.scene.rotation.y) * 0.15;
  }

  Object.keys(target).forEach((key) => lerpTo(key, target[key]));

  const applyRot = (name, x, y, z) => {
    const node = bones[name];
    if (!node) return;
    const rest = restRot[name];
    node.rotation.set(rest.x + x, rest.y + (y || 0), rest.z + (z || 0));
  };

  applyRot("head", pose.headX, pose.headY, pose.headZ);
  applyRot("neck", pose.headX * 0.45, pose.headY * 0.4, pose.headZ * 0.4);
  applyRot("spine", pose.spineX * 0.6, 0, pose.spineZ);
  applyRot("chest", pose.spineX * 0.35, 0, pose.spineZ * 0.5);
  applyRot("hips", pose.hipsX, 0, 0);
  applyRot("leftUpperArm", 0, 0, pose.armL);
  applyRot("rightUpperArm", 0, 0, -pose.armR);
  applyRot("leftLowerArm", 0, 0, pose.armL * 0.35);
  applyRot("rightLowerArm", 0, 0, -pose.armR * 0.35);

  vrm.scene.position.y = modelBaseY + pose.groupY;

  /* 眨眼 */
  const em = vrm.expressionManager;
  if (em) {
    if (state === "sleeping") {
      blinkValue = 1;
    } else {
      const now = t;
      if (blinkStartedAt < 0 && now > nextBlinkAt) blinkStartedAt = now;
      if (blinkStartedAt >= 0) {
        const p = now - blinkStartedAt;
        if (p < 0.055) blinkValue = p / 0.055;
        else if (p < 0.11) blinkValue = 1 - (p - 0.055) / 0.055;
        else {
          blinkValue = 0;
          blinkStartedAt = -1;
          nextBlinkAt = now + 2.6 + Math.random() * 3.2;
        }
      } else {
        blinkValue += (0 - blinkValue) * 0.4;
      }
    }

    em.setValue("blink", blinkValue);
    em.setValue(
      "happy",
      Math.max(actionHappy, state === "happy" ? 0.55 : state === "attention" ? 0.25 : 0)
    );
    em.setValue("surprised", state === "error" ? 0.5 : state === "attention" ? 0.35 : 0);
    em.setValue("sad", state === "sleeping" ? 0.05 : 0);
  }

  /* 配饰跟随 */
  const headWorld = new THREE.Vector3();
  if (headBone) {
    headBone.getWorldPosition(headWorld);
    if (clip) {
      clip.position.copy(headWorld);
      clip.position.x -= 0.085;
      clip.position.y += 0.045;
      clip.position.z += 0.045;
      clip.rotation.y = -0.35 + Math.sin(t * 0.8) * 0.05;
      clip.rotation.z = 0.25 + pose.headZ * 0.5;
    }
  }
  const chestWorld = new THREE.Vector3();
  if (chestBone) {
    chestBone.getWorldPosition(chestWorld);
    if (plush) {
      plush.position.set(
        chestWorld.x,
        chestWorld.y - 0.03,
        chestWorld.z + 0.115 + Math.sin(t * 1.2) * 0.003
      );
      plush.rotation.y = Math.sin(t * 0.6) * 0.08;
      plush.rotation.z = Math.sin(t * 0.9) * 0.05;
    }
  }
  if (neckBone && collarSet) {
    const w = new THREE.Vector3();
    neckBone.getWorldPosition(w);
    collarSet.position.set(w.x, w.y + 0.005, w.z);
    collarSet.rotation.z = pose.headZ * 0.4;
  }
  if (hipsBone && apronMesh) {
    const w = new THREE.Vector3();
    hipsBone.getWorldPosition(w);
    apronMesh.position.set(w.x, w.y - 0.115, w.z + 0.095);
    apronMesh.rotation.z = pose.spineZ * 0.6;
    apronMesh.rotation.y = Math.sin(t * 0.6) * 0.03;
  }

  vrm.update(dt);
}

function updateBubbles(dt, t) {
  bubbles.forEach((sprite) => {
    sprite.position.y += sprite.userData.speed * dt;
    sprite.position.x += Math.sin(t * 0.9 + sprite.userData.wobble) * 0.0006;
    if (sprite.position.y > 0.75) {
      sprite.position.y = -0.4;
      sprite.position.x = (Math.random() - 0.5) * 0.85;
      sprite.position.z = (Math.random() - 0.2) * 0.4;
    }
  });
}

/* ------------------------------------------------------------------ */
/* 气泡 UI                                                             */
/* ------------------------------------------------------------------ */

const SOURCE_LABEL = { qq: "QQ", wechat: "微信", local: "本机", dsh: "DSH", pet: "桌宠" };
const SOURCE_COLOR = { qq: "#2f7cf6", wechat: "#23b26a", local: "#7f8fb5", dsh: "#8a63f5", pet: "#5aa8e8" };

let bubbleHideTimer = null;
let typeTimer = null;
let swapTimer = null;
let hideTimer = null;
let bubbleWidth = 240;
let hintHidden = false;

function setMeta(source, state) {
  const label = SOURCE_LABEL[source] || "桌宠";
  const color = SOURCE_COLOR[source] || "#7f8fb5";
  bubbleMetaEl.innerHTML = `<span class="dot" style="background:${color};box-shadow:0 0 6px ${color}"></span>${label} · ${state}`;
}

function typeText(text) {
  if (typeTimer) clearInterval(typeTimer);
  bubbleTextEl.classList.toggle("long", text.length > 34);
  bubbleTextEl.textContent = "";
  let i = 0;
  typeTimer = setInterval(() => {
    i += 2;
    bubbleTextEl.textContent = text.slice(0, i);
    if (i >= text.length) {
      clearInterval(typeTimer);
      typeTimer = null;
      bubbleTextEl.textContent = text;
    }
  }, 38);
}

function showBubble(text, source, state) {
  if (!text) return;
  const wasHidden = bubbleEl.classList.contains("hidden");
  bubbleEl.classList.remove("hidden");
  if (hideTimer) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }
  setMeta(source, state);

  const life = Math.min(14000, Math.max(7000, 4200 + text.length * 45));
  bubbleEl.style.setProperty("--life", `${life}ms`);
  bubbleEl.classList.remove("bar-run");
  void bubbleEl.offsetWidth;
  bubbleEl.classList.add("bar-run");

  const applyText = () => {
    typeText(text);
    bubbleEl.classList.remove("swap");
    requestAnimationFrame(() => {
      bubbleWidth = bubbleEl.offsetWidth || 240;
    });
  };
  if (!wasHidden && bubbleTextEl.textContent.trim()) {
    bubbleEl.classList.add("swap");
    if (swapTimer) clearTimeout(swapTimer);
    swapTimer = setTimeout(applyText, 120);
  } else {
    applyText();
  }

  requestAnimationFrame(() => bubbleEl.classList.add("show"));
  if (bubbleHideTimer) clearTimeout(bubbleHideTimer);
  bubbleHideTimer = setTimeout(() => {
    bubbleEl.classList.remove("show");
    hideTimer = setTimeout(() => bubbleEl.classList.add("hidden"), 260);
  }, life + 800);
}

function placeBubble() {
  if (!ready || !headBone || bubbleEl.classList.contains("hidden")) return;
  const v = new THREE.Vector3();
  headBone.getWorldPosition(v);
  v.y += 0.13;
  v.project(camera);
  const rawX = (v.x * 0.5 + 0.5) * window.innerWidth;
  const x = Math.min(Math.max(rawX, 120), window.innerWidth - 120);
  const y = Math.min(Math.max((-v.y * 0.5 + 0.5) * window.innerHeight, 70), window.innerHeight - 40);
  bubbleEl.style.setProperty("--bx", `${x}px`);
  bubbleEl.style.setProperty("--by", `${y}px`);
  const tail = Math.min(82, Math.max(18, 50 + ((rawX - x) / bubbleWidth) * 100));
  bubbleEl.style.setProperty("--tail", `${tail}%`);
}

/* ------------------------------------------------------------------ */
/* 状态接收                                                            */
/* ------------------------------------------------------------------ */

if (pet) {
  pet.onState((payload) => {
    if (!payload) return;
    activeState = payload.state || "idle";
    activeSource = payload.source || "local";
    if (payload.action) playAction(payload.action, 1.7);
    zzzEl.classList.toggle("hidden", activeState !== "sleeping");
    const stateLabel = {
      idle: "空闲",
      working: "回复中",
      thinking: "思考中",
      sleeping: "睡着了",
      happy: "开心",
      attention: "注意到你",
      error: "有点慌",
    }[activeState] || activeState;
    showBubble(payload.pet_text, payload.source, stateLabel);
  });

  pet.onSnapshot(() => {
    document.body.classList.add("snap");
    document.getElementById("hint").classList.remove("hidden");
  });
}

/* ------------------------------------------------------------------ */
/* 鼠标：穿透 + 拖动 + 右键菜单                                        */
/* ------------------------------------------------------------------ */

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let ignoreMouse = true;
let dragging = false;
let lastScreenX = 0;
let lastScreenY = 0;
let lastHitAt = 0;
let pressInfo = null;
let pokeIndex = 0;
let lastPokeAt = 0;

const POKE_LINES = [
  "欸嘿～被戳到啦！",
  "主人的手指好暖…",
  "再戳一下嘛，泡泡都冒出来了。",
  "唔…人家在忙啦，不过可以陪你一下下。",
  "被你发现啦，今天也要开心哦。",
];
const POKE_ACTIONS = ["wave", "hop", "shy", "nod"];

function poke() {
  const now = performance.now();
  if (now - lastPokeAt < 700) return;
  lastPokeAt = now;
  const line = POKE_LINES[pokeIndex % POKE_LINES.length];
  const action = POKE_ACTIONS[pokeIndex % POKE_ACTIONS.length];
  pokeIndex += 1;
  playAction(action, 1.7);
  spawnHearts(6);
  showBubble(line, "pet", "戳戳");
}

function setIgnore(next) {
  if (next === ignoreMouse) return;
  ignoreMouse = next;
  if (pet) pet.setIgnore(next);
}

window.addEventListener("pointermove", (e) => {
  pointer.x = (e.clientX / window.innerWidth) * 2 - 1;
  pointer.y = -(e.clientY / window.innerHeight) * 2 + 1;
  lastPointerAt = performance.now();

  lookTarget.position.set(pointer.x * 0.45, 0.42 + pointer.y * 0.18, 1.2);

  if (dragging) {
    if (pet) pet.drag(e.screenX - lastScreenX, e.screenY - lastScreenY);
    lastScreenX = e.screenX;
    lastScreenY = e.screenY;
    return;
  }

  if (!ready || !vrm) {
    setIgnore(true);
    return;
  }

  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObject(vrm.scene, true);
  const overModel = hits.some((h) => h.object && h.object.isMesh);
  const overMenu = !menuEl.classList.contains("hidden") &&
    menuEl.getBoundingClientRect().left <= e.clientX &&
    e.clientX <= menuEl.getBoundingClientRect().right &&
    menuEl.getBoundingClientRect().top <= e.clientY &&
    e.clientY <= menuEl.getBoundingClientRect().bottom;

  if (overModel || overMenu) {
    lastHitAt = performance.now();
    setIgnore(false);
  } else if (performance.now() - lastHitAt > 120) {
    setIgnore(true);
  }
});

window.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  if (ignoreMouse) return;
  if (e.target && e.target.closest && e.target.closest("#menu")) return;
  pressInfo = { x: e.clientX, y: e.clientY, at: performance.now() };
  dragging = true;
  lastScreenX = e.screenX;
  lastScreenY = e.screenY;
  if (!hintHidden) {
    hintHidden = true;
    hintEl.classList.add("hide");
  }
});

window.addEventListener("pointerup", (e) => {
  dragging = false;
  if (pressInfo) {
    const dx = e.clientX - pressInfo.x;
    const dy = e.clientY - pressInfo.y;
    const moved = Math.sqrt(dx * dx + dy * dy);
    const held = performance.now() - pressInfo.at;
    pressInfo = null;
    if (moved < 7 && held < 600) poke();
  }
});

window.addEventListener("contextmenu", (e) => {
  e.preventDefault();
  const w = 160;
  const h = 168;
  menuEl.style.left = `${Math.max(4, Math.min(e.clientX, window.innerWidth - w - 4))}px`;
  menuEl.style.top = `${Math.max(4, Math.min(e.clientY, window.innerHeight - h - 4))}px`;
  menuEl.classList.remove("hidden");
});

menuEl.addEventListener("click", (e) => {
  const act = e.target && e.target.dataset && e.target.dataset.act;
  if (!act) return;
  menuEl.classList.add("hidden");
  if (act === "quit" && pet) pet.quit();
  else if (act === "reset" && pet) pet.resetPosition();
  else if (act === "test" && pet) pet.testBubble();
  else if (act === "poke") poke();
});

window.addEventListener("pointerdown", (e) => {
  if (!menuEl.classList.contains("hidden") && !menuEl.contains(e.target)) {
    menuEl.classList.add("hidden");
  }
});

/* ------------------------------------------------------------------ */
/* 主循环                                                              */
/* ------------------------------------------------------------------ */

function tick() {
  const dt = Math.min(clock.getDelta(), 0.05);
  const t = performance.now() / 1000 - t0 / 1000;
  try {
    updateAnimation(dt, t);
  } catch (err) {
    /* 保持桌宠不崩 */
  }
  updateBubbles(dt, t);
  updateHearts(dt);
  placeBubble();
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}

if (pet) pet.setIgnore(true);
tick();

setTimeout(() => {
  if (!hintHidden) {
    hintEl.classList.remove("hidden");
    hintEl.textContent = "戳戳我 · 拖动 · 右键菜单";
    setTimeout(() => hintEl.classList.add("hide"), 3200);
  }
}, 3800);
