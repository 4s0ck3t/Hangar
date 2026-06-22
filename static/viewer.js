// Hangar three.js viewer (GLB/GLTF + FBX) — mounts into the drawer preview area.
import * as THREE from '/three.min.js';
import { GLTFLoader } from '/GLTFLoader.js';
import { FBXLoader } from '/FBXLoader.js';
import { OrbitControls } from '/OrbitControls.js';

let renderer, scene, camera, controls, animId, resizeObs;

export function startViewer(container, assetId, ext) {
  destroyViewer();

  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;';
  container.innerHTML = '';
  container.style.position = 'relative';
  container.appendChild(canvas);

  const spinner = document.createElement('div');
  spinner.className = 'v3d-loading';
  spinner.textContent = 'Loading 3D…';
  container.appendChild(spinner);

  const w = container.clientWidth || 380;
  const h = container.clientHeight || 380;

  // preserveDrawingBuffer so we can snapshot the canvas to cache a thumbnail.
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(w, h);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.2;

  scene = new THREE.Scene();
  scene.background = new THREE.Color(0xf3f4f6);  // light neutral — reads as white

  scene.add(new THREE.HemisphereLight(0xffffff, 0x334455, 2.0));
  const sun = new THREE.DirectionalLight(0xfff5e0, 2.5);
  sun.position.set(3, 6, 4);
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0x8899cc, 0.8);
  fill.position.set(-4, 1, -3);
  scene.add(fill);

  camera = new THREE.PerspectiveCamera(45, w / h, 0.001, 2000);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.06;

  // Mount a loaded object (GLTF scene or FBX group), frame it, wire animations.
  const onLoaded = (object, animations) => {
    spinner.remove();
    scene.add(object);

    const box = new THREE.Box3().setFromObject(object);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    object.position.sub(center);

    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const fovRad = camera.fov * Math.PI / 180;
    const dist = (maxDim / 2) / Math.tan(fovRad / 2) * 1.7;
    camera.position.set(dist * 0.7, dist * 0.45, dist);
    camera.near = maxDim * 0.001;
    camera.far = maxDim * 300;
    camera.updateProjectionMatrix();
    controls.target.set(0, 0, 0);
    controls.update();

    if (animations?.length) {
      const clock = new THREE.Clock();
      const mixer = new THREE.AnimationMixer(object);
      mixer.clipAction(animations[0]).play();
      scene.userData._mixer = mixer;
      scene.userData._clock = clock;
    }

    // Snapshot the framed model and cache it as this asset's thumbnail, so the
    // grid shows a real preview and re-opening is instant. Once only.
    if (assetId != null && !scene.userData._snapped) {
      scene.userData._snapped = true;
      setTimeout(() => {
        try {
          renderer.render(scene, camera);  // fresh frame in the preserved buffer
          const dataUrl = renderer.domElement.toDataURL('image/jpeg', 0.85);
          fetch(`/api/assets/${assetId}/thumb`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ image: dataUrl }),
          }).then(() => { if (window.onViewerThumbCached) window.onViewerThumbCached(assetId); })
            .catch(() => {});
        } catch (_) { /* capture not available — skip */ }
      }, 450);
    }
  };

  const onError = (err) => {
    spinner.textContent = 'Preview unavailable';
    console.warn('[Hangar viewer]', err);
  };

  const url = `/api/assets/${assetId}/file`;
  if ((ext || '').toLowerCase() === '.fbx') {
    // FBXLoader passes back the THREE.Group directly; clips live on group.animations.
    new FBXLoader().load(url, (group) => onLoaded(group, group.animations), undefined, onError);
  } else {
    new GLTFLoader().load(url, (gltf) => onLoaded(gltf.scene, gltf.animations), undefined, onError);
  }

  function animate() {
    animId = requestAnimationFrame(animate);
    controls.update();
    if (scene?.userData._mixer) {
      scene.userData._mixer.update(scene.userData._clock.getDelta());
    }
    renderer.render(scene, camera);
  }
  animate();

  resizeObs = new ResizeObserver(() => {
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    if (cw > 0 && ch > 0) {
      renderer.setSize(cw, ch);
      camera.aspect = cw / ch;
      camera.updateProjectionMatrix();
    }
  });
  resizeObs.observe(container);
}

export function destroyViewer() {
  if (animId) { cancelAnimationFrame(animId); animId = null; }
  if (resizeObs) { resizeObs.disconnect(); resizeObs = null; }
  if (controls) { controls.dispose(); controls = null; }
  if (renderer) { renderer.dispose(); renderer = null; }
  scene = null;
  camera = null;
}
