import os, json, pytest
from mcp_service import *

@pytest.fixture(scope="session",autouse=True)
def dataset():
    data=[{"SectionId":"1","SectionName":"CS 262-01","CourseTitle":"Software Eng","CourseDescription":"Testing and teamwork.","Department":"CS","MeetingTime":"MWF 09:30"}]
    with open("sections.json","w")as f:json.dump(data,f)
    os.environ["SECTIONS_PATH"]="sections.json"

def test_find_courses():
    r=find_courses("Testing");assert r and r[0]["course_title"]

def test_department():
    r=find_sections_by_department("CS");assert all("CS" in x["section_name"] for x in r)

def test_level():
    r=find_sections_by_level("200");assert isinstance(r,list)

def test_time():
    r=find_sections_by_time("morning");assert isinstance(r,list)

def test_get_details():
    sid=find_courses("Testing")[0]["section_id"];assert get_section_details(sid)