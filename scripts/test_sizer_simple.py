import asyncio
from unittest.mock import MagicMock
from src.agents.sizer import run_sizer_node
from src.orchestrator.state import OrchestratorState, WorkloadProfile, ComponentProfile, Category, WorkloadRequest, WorkloadRequirement, CloudProvider

async def test_sizer_basic():
    print("Testing sizer agent...")
    llm = MagicMock()
    pricing_service = MagicMock()
    
    # Mock pricing_service.get_skus to return empty list or some dummy data
    pricing_service.get_skus.return_value = []
    
    state: OrchestratorState = {
        "messages": [],
        "workload_request": WorkloadRequest(
            project_name="Test",
            environment="dev",
            tier="standard",
            target_providers=[CloudProvider.AWS],
            workloads=[WorkloadRequirement(name="web", category="compute", description="web server")]
        ),
        "workload_profile": WorkloadProfile(
            request_id="test",
            environment="dev",
            tier="standard",
            components=[ComponentProfile(
                workload_name="web",
                resolved_category=Category.COMPUTE,
                vcpus=2,
                memory_gb=4,
                storage_gb=20,
                requires_gpu=False,
                instance_families=["m5"],
                rationale="test"
            )]
        ),
        "request_id": "test-123"
    }
    
    try:
        updated_state = await run_sizer_node(state, llm, pricing_service)
        print("Sizer node ran successfully!")
        print(f"Messages: {len(updated_state['messages'])}")
        return True
    except Exception as e:
        print(f"Sizer node failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = asyncio.run(test_sizer_basic())
    if success:
        print("Results: 1/1 passed, 0 failed")
    else:
        print("Results: 0/1 passed, 1 failed")
