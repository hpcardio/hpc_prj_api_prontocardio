from app_prontocardio.app import app


def test_external_last_attendance_route_is_exposed_without_removing_internal_history():
    paths = app.openapi()["paths"]
    external_path = (
        "/agendamentos/pacientes/{cd_paciente}/ultimo-atendimento"
    )

    assert external_path in paths
    assert "get" in paths[external_path]
    assert (
        "/agendamentos/pacientes/{cd_paciente}/historico-agendamentos"
        in paths
    )


if __name__ == "__main__":
    test_external_last_attendance_route_is_exposed_without_removing_internal_history()
    print("ultimo atendimento route contract: ok")
