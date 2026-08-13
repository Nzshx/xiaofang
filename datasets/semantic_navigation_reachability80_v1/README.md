# Semantic Navigation Reachability-80 Dataset v1

This dataset references three refined fire-inspection pipeline runs.  It does
not copy their large source or review files.  A floor is included only when it
contains inspection targets and its refined same-floor pairwise target
reachability is at least 0.80.

## Included samples

- Neighbor-center run: F1 and F2.
- Test2 run: B1, B2, F1, F2, F3, F4, F5, F6, and ROOF.
- Garage run: B1 and B2.

The resulting development dataset contains 13 floor graphs, 2,931 inspection
targets, 1,056 Area nodes, 365 segmentation Portals, 105 connector Portals,
61,642 refined physical nodes, and 82,141 refined physical edges.

## Refined physical graph

The authoritative physical graph is
`area_graph_navigation_refined/refined_navigation_graph.json`.  Connector
Portals are represented by `connector_portal` nodes and
`connector_portal_edge` edges.  Every refinement run was accepted with zero
strict geometry-validation failures.

## Clearance

For a standard Portal, `clearance` is the free-space distance-transform value
at the Portal center multiplied by raster pixel size.  It approximates the
shortest distance from the Portal center to a blocked cell or obstacle.  It is
a local free-space safety margin, while `width` is the measured cross-section
of the passage; the two values are related but are not interchangeable.

Connector Portals currently provide `gap_distance`, evidence type, and
confidence, but do not provide a physical `width` or `clearance`.  The first
edge-gated model must use a separate connector relation encoder and explicit
missing-value masks.  It must not treat `gap_distance` as clearance.

## First model and supervision policy

- Standard Portal relations: gate with normalized width, clearance,
  bottleneck, and confidence.
- Connector Portal relations: gate with normalized gap distance, optional
  evidence distance, confidence, and evidence type.
- Ordinary physical navigation edges: use normalized length only.
- Target-neighbor relations: regenerate with the current 15-percent
  neighborhood implementation.

Training begins with self-supervised heterogeneous-graph pretraining.  A
component-constrained open selective-TSP teacher then produces top-K solution
pools for distance tolerances 0, 5, and 10 percent.  Only selected consecutive
legs are physically checked on the refined navigation graph.  Soft node and
transition labels come from top-K occurrence frequencies.  A later, small
pairwise route-preference audit is sufficient; full manually drawn routes are
not required.

All samples remain in the development split until more independent buildings
are available.  Floors from one source run must never be randomly split across
training and testing.
