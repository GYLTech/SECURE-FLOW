from typing import List, Optional
from pydantic import BaseModel, Field


class FIRDetails(BaseModel):
    PoliceStation: str
    FIRNumber: str
    Year: str


class ActsAndSection(BaseModel):
    acts: str
    section: str


class CaseHistoryItem(BaseModel):
    judge: str
    businessOnDate: str
    hearingDate: str
    purpose: str
    inputType: str
    lawyerRemark: Optional[str]


class OrderItem(BaseModel):
    order_number: str
    order_date: str
    order_link: str



class CaseDocument(BaseModel):
    case_no: str
    cino: str
    court_code: str
    state_code: str
    dist_code: str
    court_type: str
    court_complex_code: str
    est_code: str
    case_type: str
    rgyear: str
    case_reg_no: str
    fir_details: FIRDetails
    CaseType: str
    FilingNumber: str
    RegistrationNumber: str
    CNRNumber: str
    e_Filno: str = Field(alias="e-Filno")
    FirstHearingDate: str
    NextHearingDate: str
    NatureofDisposal : str
    CaseStage: str
    CourtNumberandJudge: str
    petitioner_and_advocate: List[str]
    respondent_and_advocate: List[str]
    actsandSection: ActsAndSection
    case_history: List[CaseHistoryItem]
    case_transfer: List[dict]
    orders: List[OrderItem]

    class Config:
        allow_population_by_field_name = True
