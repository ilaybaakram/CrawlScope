from collections import deque


def run_bfs_demo(graph: dict[str, list[str]], start_page: str) -> None:
    queue = deque([start_page])
    visited = {start_page}
    traversal_order = []

    while queue:
        current_page = queue.popleft()
        traversal_order.append(current_page)
        print(f"Visiting page: {current_page}")

        for neighbor_page in graph[current_page]:
            if neighbor_page not in visited:
                visited.add(neighbor_page)
                queue.append(neighbor_page)

    print(f"\nFinal BFS Traversal Order: {' -> '.join(traversal_order)}")


if __name__ == "__main__":
    website_graph = {
        "A": ["B", "C"],
        "B": ["D", "E"],
        "C": ["F"],
        "D": [],
        "E": [],
        "F": [],
    }
    run_bfs_demo(website_graph, "A")

