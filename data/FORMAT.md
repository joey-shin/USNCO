# questions.js Formatting


- General Format:
```
{
    id: "", // string: ID of question
    exam_type: "", // string: "local", "national-I", "national-II", "national-III"
    year: , // number: year of exam
    number: , // number: question number
    topic: "", // string: One of 10 topics listed in USNCO website "Stoichiometry/Solutions", "Descriptive/Laboratory", "States of Matter", "Thermodynamics", "Kinetics", "Equilibrium", "Oxidation - Reduction", " Atomic Structure/Periodicity", "Bonding/Molecular Structure", "Organic/Biochemistry"
    question: "", // string: question written out
    question_image: // boolean: True if question includes an image, False if purely text based
    answer: "" // string: Correct answer "A", "B", "C", or "D"
    answer_image: //boolean: True if answer includes an image, False if purely text based
    choices: { // string if question is text based, null if image based
        A: "", 
        B: "",
        C: "",
        D: ""
    },
    image: {
        image: [], // list of strings: directory or directories to the full image of question and answers
        question_image: [], // list of strings: direcotry or directories of image of just the question
        subtext: [] // list of strings or null: directory or directories of image of subtext for problems refering to common subtext
    }
    solution: {
        text: "", // string or null: text of solution if seperate solution document exists, null if not
        image: [] //list of strings or null: directory or directories to the image of corresponding solution from solution document if exists, null if not
    }
}
```

- Example

```
{
    id: "2021_local-I_59",
    exam_type: "local",
    year: 2021,
    number: 59,
    topic: "Organic/Biochemsitry",
    question: "Which Conformation of cyclohexane \ce{C6H12} is most stable?",
    question_image: False,
    answer: "B",
    answer_image: True,
    choices: {
        A: null,
        B: null,
        C: null,
        D: null
    },
    images: {
        image: ["/images/local/2021_local-I_59.png"],
        question_image: ["/images/local/2021_local-I_59_question.png"], 
        subtext: null
    }
    solution: {
        text: "The chair conformation, or B is the most stable conformation for cyclohexane, as it does not have any steric hindrance or steric repulsion between the hydrogen bonds.",
        image: ["/images/local/2021_local-I_59_solution.png"]
    }
}
```
