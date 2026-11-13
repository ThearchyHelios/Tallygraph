import type { Connection, Edge, EdgeChange, Node, NodeChange } from '@xyflow/react'

type FlowHelpers = {
  applyNodeChanges: <NodeType extends Node = Node>(changes: NodeChange<NodeType>[], nodes: NodeType[]) => NodeType[]
  applyEdgeChanges: <EdgeType extends Edge = Edge>(changes: EdgeChange<EdgeType>[], edges: EdgeType[]) => EdgeType[]
  addEdge: <EdgeType extends Edge = Edge>(edgeParams: EdgeType | Connection, edges: EdgeType[]) => EdgeType[]
}

function applySingleChange(change: any, element: any): void {
  switch (change.type) {
    case 'select':
      element.selected = change.selected
      break
    case 'position':
      if (typeof change.position !== 'undefined') element.position = change.position
      if (typeof change.dragging !== 'undefined') element.dragging = change.dragging
      break
    case 'dimensions':
      if (typeof change.dimensions !== 'undefined') {
        element.measured = { ...change.dimensions }
        if (change.setAttributes) {
          if (change.setAttributes === true || change.setAttributes === 'width') {
            element.width = change.dimensions.width
          }
          if (change.setAttributes === true || change.setAttributes === 'height') {
            element.height = change.dimensions.height
          }
        }
      }
      if (typeof change.resizing === 'boolean') element.resizing = change.resizing
      break
  }
}

function applyChangesFallback<Item extends { id: string }>(changes: any[], elements: Item[]): Item[] {
  const updatedElements: Item[] = []
  const changesMap = new Map<string, any[]>()
  const addItemChanges: any[] = []

  for (const change of changes) {
    if (change.type === 'add') {
      addItemChanges.push(change)
    } else if (change.type === 'remove' || change.type === 'replace') {
      changesMap.set(change.id, [change])
    } else {
      const list = changesMap.get(change.id)
      if (list) list.push(change)
      else changesMap.set(change.id, [change])
    }
  }

  for (const element of elements) {
    const queued = changesMap.get(element.id)
    if (!queued) {
      updatedElements.push(element)
      continue
    }
    if (queued[0].type === 'remove') continue
    if (queued[0].type === 'replace') {
      updatedElements.push({ ...queued[0].item })
      continue
    }
    const copy = { ...element }
    for (const change of queued) applySingleChange(change, copy)
    updatedElements.push(copy)
  }

  for (const change of addItemChanges) {
    if (change.index !== undefined) updatedElements.splice(change.index, 0, { ...change.item })
    else updatedElements.push({ ...change.item })
  }
  return updatedElements
}

function addEdgeFallback<EdgeType extends Edge = Edge>(
  edgeParams: EdgeType | Connection,
  edges: EdgeType[],
): EdgeType[] {
  if (!edgeParams.source || !edgeParams.target) return edges
  const sourceHandle = edgeParams.sourceHandle ?? null
  const targetHandle = edgeParams.targetHandle ?? null
  const exists = edges.some(
    (e) => e.source === edgeParams.source
      && e.target === edgeParams.target
      && (e.sourceHandle ?? null) === sourceHandle
      && (e.targetHandle ?? null) === targetHandle,
  )
  if (exists) return edges
  const id = ('id' in edgeParams && edgeParams.id)
    ? edgeParams.id
    : `xy-edge__${edgeParams.source}${sourceHandle || ''}-${edgeParams.target}${targetHandle || ''}`
  return [...edges, { ...edgeParams, id, sourceHandle, targetHandle } as EdgeType]
}

let impl: FlowHelpers = {
  applyNodeChanges: applyChangesFallback,
  applyEdgeChanges: applyChangesFallback,
  addEdge: addEdgeFallback,
}

export function registerFlowHelpers(next: FlowHelpers): void {
  impl = next
}

export function applyNodeChanges<NodeType extends Node = Node>(
  changes: NodeChange<NodeType>[],
  nodes: NodeType[],
): NodeType[] {
  return impl.applyNodeChanges(changes, nodes)
}

export function applyEdgeChanges<EdgeType extends Edge = Edge>(
  changes: EdgeChange<EdgeType>[],
  edges: EdgeType[],
): EdgeType[] {
  return impl.applyEdgeChanges(changes, edges)
}

export function addEdge<EdgeType extends Edge = Edge>(
  edgeParams: EdgeType | Connection,
  edges: EdgeType[],
): EdgeType[] {
  return impl.addEdge(edgeParams, edges)
}
